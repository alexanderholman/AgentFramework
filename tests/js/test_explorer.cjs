'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const explorer = require('../../src/af/views/assets/explorer.js')

class Element {
  constructor(tag = 'input') {
    this.tag = tag
    this.value = ''
    this.textContent = ''
    this.dataset = {}
    this.children = []
    this.events = {}
    this.attributes = {}
  }
  addEventListener(name, fn) { this.events[name] = fn }
  setAttribute(name, value) { this.attributes[name] = value }
  replaceChildren() { this.children = [] }
  append(...values) { this.children.push(...values) }
  querySelectorAll(tag) { return this.children.filter(item => item.tag === tag) }
  focus() { this.focused = true }
  set innerHTML(_) { throw new Error('Untrusted markup must never be inserted') }
}

function document(kind = 'rest') {
  const elements = {}
  const ids = ['explorer-form', 'send-request', 'cancel-request', 'request-status',
    'response-body', 'response-headers', 'response-region', 'operation', 'path-parameters',
    'query-parameters', 'request-body', 'bearer', 'condition', 'idempotency', 'clear-request',
    'graphql-query', 'graphql-variables', 'operation-name']
  for (const id of ids) elements[id] = new Element()
  elements.operation.value = 'GET /api/v1/tasks'
  elements['query-parameters'].value = '{}'
  elements['graphql-variables'].value = '{}'
  return {body: {dataset: {explorer: kind}}, elements,
    getElementById: id => elements[id], createElement: tag => new Element(tag)}
}

test('REST builders encode values without accepting remote or traversing targets', () => {
  const request = explorer.buildRestRequest('GET /api/v1/tasks/{task_id}', {task_id: 'α'},
    '{"limit":10,"tag":["one","two"]}', 'ignored')
  assert.deepEqual(request, {path: '/api/v1/tasks/%CE%B1?limit=10&tag=one&tag=two', method: 'GET', body: undefined})
  for (const operation of ['POST https://remote.test/api', 'GET //remote.test/api', 'GET /api/../secret']) {
    assert.throws(() => explorer.buildRestRequest(operation, {}, '{}', ''), /installed/)
  }
  for (const task_id of ['', '..', '../other', 'x/y', 'x\\y', 'x?y', 'x#y']) {
    assert.throws(() => explorer.buildRestRequest('GET /api/v1/tasks/{task_id}', {task_id}, '{}', ''), /valid/)
  }
  for (const query of ['{', '[]', 'null', '{"x":{}}', '{"x":null}']) {
    assert.throws(() => explorer.buildRestRequest('GET /api/v1/tasks', {}, query, ''))
  }
  assert.throws(() => explorer.buildRestRequest('POST /api/v1/tasks', {}, '{}', '{'), /valid JSON/)
})

test('GraphQL preserves explicit document and variables without hidden registration', () => {
  const result = explorer.buildGraphQLRequest('query Read { tasks { id } }', '{"limit":2}', ' Read ')
  assert.equal(result.path, '/graphql')
  assert.deepEqual(JSON.parse(result.body), {query: 'query Read { tasks { id } }', variables: {limit: 2}, operationName: 'Read'})
  assert.throws(() => explorer.buildGraphQLRequest(' ', '{}', ''), /Enter/)
  assert.throws(() => explorer.buildGraphQLRequest('query { tasks { id } }', '[]', ''), /object/)
})

test('only an explicit submission dispatches, with same-origin nonredirecting credentials', async () => {
  const doc = document()
  const fields = doc.elements
  const calls = []
  explorer.attach(doc, async (path, options) => {
    calls.push({path, options})
    return new Response('{"output":"<script>alert(1)</script>"}', {status: 200, headers: {ETag: '"abc"'}})
  })
  assert.equal(calls.length, 0)
  assert.equal(fields['send-request'].disabled, false)
  fields.bearer.value = 'private-credential'
  fields.condition.value = '"previous"'
  fields.idempotency.value = 'human-chosen-key'
  await fields['explorer-form'].events.submit({preventDefault() {}})
  assert.equal(calls.length, 1)
  assert.equal(calls[0].path, '/api/v1/tasks')
  assert.equal(calls[0].options.redirect, 'error')
  assert.equal(calls[0].options.credentials, 'same-origin')
  assert.equal(calls[0].options.headers.Authorization, 'Bearer private-credential')
  assert.equal(calls[0].options.headers['If-Match'], '"previous"')
  assert.match(fields['response-body'].textContent, /<script>/)
  assert.equal(fields['response-headers'].textContent, 'HTTP 200\nETag: "abc"')
  assert.equal(fields['response-region'].attributes['aria-busy'], 'false')
  fields['clear-request'].events.click()
  assert.equal(fields.bearer.value, '')
  assert.equal(fields['response-body'].textContent, '')
})

test('dynamic path controls are named and invalid input never dispatches', async () => {
  const doc = document()
  let calls = 0
  explorer.attach(doc, async () => { calls++; return new Response('{}') })
  doc.elements.operation.value = 'PUT /api/v1/tasks/{task_id}'
  doc.elements.operation.events.change()
  const [label, input] = doc.elements['path-parameters'].children
  assert.equal(label.htmlFor, input.id)
  assert.equal(input.required, true)
  await doc.elements['explorer-form'].events.submit({preventDefault() {}})
  assert.equal(calls, 0)
  input.value = 'known-task'
  doc.elements['request-body'].value = '{"prompt":"hello"}'
  await doc.elements['explorer-form'].events.submit({preventDefault() {}})
  assert.equal(calls, 1)
})

test('aborting waiting does not claim rollback or retry', async () => {
  const doc = document('graphql')
  doc.elements['graphql-query'].value = 'mutation { createTask(input: {prompt: "hello"}) { id } }'
  let calls = 0
  explorer.attach(doc, (_, options) => new Promise((resolve, reject) => {
    calls++
    options.signal.addEventListener('abort', () => reject(new Error('aborted')))
  }))
  const pending = doc.elements['explorer-form'].events.submit({preventDefault() {}})
  await doc.elements['explorer-form'].events.submit({preventDefault() {}})
  doc.elements['cancel-request'].events.click()
  await pending
  assert.equal(calls, 1)
  assert.match(doc.elements['request-status'].textContent, /may still complete/)
  assert.equal(doc.elements['send-request'].disabled, false)
})

test('response reader bounds allocation and closes its reader', async () => {
  let cancelled = false
  let released = false
  await assert.rejects(explorer.readResponse({body: {getReader: () => ({
    read: async () => ({done: false, value: new Uint8Array(1024 * 1024 + 1)}),
    cancel: async () => { cancelled = true }, releaseLock: () => { released = true }
  })}}), /limit/)
  assert.equal(cancelled, true)
  assert.equal(released, true)
})
