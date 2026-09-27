"use strict";

const AFExplorer = (() => {
  const MAX_BYTES = 1024 * 1024;
  function jsonObject(text, label) {
    let value;
    try { value = JSON.parse(text || "{}"); }
    catch { throw new Error(`${label} must be valid JSON.`); }
    if (value === null || Array.isArray(value) || typeof value !== "object") {
      throw new Error(`${label} must be a JSON object.`);
    }
    return value;
  }

  function buildRestRequest(operation, parameters, query, body) {
    const match = /^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (\/api\/[A-Za-z0-9_{}./:-]+)$/.exec(operation);
    if (!match || match[2].split("/").some(part => part === "." || part === "..")) {
      throw new Error("Choose an installed API operation.");
    }
    const method = match[1];
    const path = match[2].replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (_, key) => {
      const value = parameters[key];
      if (typeof value !== "string" || !value || value.length > 256 ||
          /[\/\\?#\s\u0000-\u001f]/.test(value) || value === "." || value === "..") {
        throw new Error(`Provide a valid ${key}.`);
      }
      return encodeURIComponent(value);
    });
    if (/[{}]/.test(path)) throw new Error("Invalid operation path.");
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(jsonObject(query, "Query parameters"))) {
      for (const item of Array.isArray(value) ? value : [value]) {
        if (!["string", "number", "boolean"].includes(typeof item)) {
          throw new Error("Query values must be text, numbers, booleans, or arrays of them.");
        }
        search.append(key, String(item));
      }
    }
    let payload;
    if (!["GET", "HEAD"].includes(method) && body.trim()) {
      try { JSON.parse(body); } catch { throw new Error("Request body must be valid JSON."); }
      payload = body;
    }
    return {path: path + (search.size ? `?${search}` : ""), method, body: payload};
  }

  function buildGraphQLRequest(query, variables, operationName) {
    if (!query.trim()) throw new Error("Enter a query or mutation.");
    const payload = {query, variables: jsonObject(variables, "Variables")};
    if (operationName.trim()) payload.operationName = operationName.trim();
    return {path: "/graphql", method: "POST", body: JSON.stringify(payload)};
  }

  async function readResponse(response) {
    if (!response.body) return "";
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let length = 0;
    let text = "";
    try {
      while (true) {
        const {done, value} = await reader.read();
        if (done) return text + decoder.decode();
        length += value.byteLength;
        if (length > MAX_BYTES) {
          await reader.cancel();
          throw new Error("Response exceeds the explorer's 1 MiB display limit.");
        }
        text += decoder.decode(value, {stream: true});
      }
    } finally { reader.releaseLock(); }
  }

  function attach(document, fetchRequest) {
    const element = id => document.getElementById(id);
    const form = element("explorer-form");
    if (!form) return;
    const send = element("send-request");
    const cancel = element("cancel-request");
    const status = element("request-status");
    const result = element("response-body");
    const responseHeaders = element("response-headers");
    const rest = document.body.dataset.explorer === "rest";
    let active = null;
    function pathFields() {
      const region = element("path-parameters");
      region.replaceChildren();
      for (const match of element("operation").value.matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)) {
        const label = document.createElement("label");
        label.htmlFor = `path-${match[1]}`;
        label.textContent = match[1];
        const input = document.createElement("input");
        input.id = label.htmlFor;
        input.required = true;
        input.maxLength = 256;
        input.dataset.parameter = match[1];
        region.append(label, input);
      }
      element("request-body").disabled = /^(GET|HEAD) /.test(element("operation").value);
    }
    if (rest) {
      element("operation").addEventListener("change", pathFields);
      pathFields();
    }
    cancel.addEventListener("click", () => active?.abort());
    element("clear-request").addEventListener("click", () => {
      element("bearer").value = "";
      result.textContent = "";
      responseHeaders.textContent = "";
      status.textContent = "Credential and displayed result cleared.";
    });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (active) return;
      let request;
      try {
        if (rest) {
          const parameters = {};
          for (const input of element("path-parameters").querySelectorAll("input")) {
            parameters[input.dataset.parameter] = input.value;
          }
          request = buildRestRequest(element("operation").value, parameters,
            element("query-parameters").value, element("request-body").value);
        } else {
          request = buildGraphQLRequest(element("graphql-query").value,
            element("graphql-variables").value, element("operation-name").value);
        }
        if (new TextEncoder().encode(request.body || "").length > MAX_BYTES) {
          throw new Error("Request exceeds the 1 MiB limit.");
        }
      } catch (error) {
        status.textContent = error.message;
        return;
      }
      active = new AbortController();
      send.disabled = true;
      cancel.disabled = false;
      element("response-region").setAttribute("aria-busy", "true");
      status.textContent = "Waiting for response…";
      result.textContent = "";
      responseHeaders.textContent = "";
      const headers = {"Accept": "application/json"};
      if (request.body !== undefined) headers["Content-Type"] = "application/json";
      if (element("bearer").value) headers.Authorization = `Bearer ${element("bearer").value}`;
      if (element("condition").value) headers["If-Match"] = element("condition").value;
      if (element("idempotency").value) headers["Idempotency-Key"] = element("idempotency").value;
      try {
        const response = await fetchRequest(request.path, {
          method: request.method, body: request.body, headers, signal: active.signal,
          credentials: "same-origin", redirect: "error", cache: "no-store"
        });
        const text = await readResponse(response);
        try { result.textContent = JSON.stringify(JSON.parse(text), null, 2); }
        catch { result.textContent = text; }
        responseHeaders.textContent = `HTTP ${response.status}`;
        if (response.headers.get("etag")) responseHeaders.textContent += `\nETag: ${response.headers.get("etag")}`;
        status.textContent = response.ok ? `Response ${response.status} received.` : `Request rejected: HTTP ${response.status}. See response.`;
      } catch (error) {
        status.textContent = active.signal.aborted ?
          "Stopped waiting. An already dispatched mutation may still complete; inspect its state before retrying." :
          "Response unavailable or too large. No automatic retry was made; inspect state before retrying a mutation.";
      } finally {
        active = null;
        send.disabled = false;
        cancel.disabled = true;
        element("response-region").setAttribute("aria-busy", "false");
        send.focus();
      }
    });
    send.disabled = false;
  }
  return {buildRestRequest, buildGraphQLRequest, readResponse, attach};
})();

if (typeof module !== "undefined") module.exports = AFExplorer;
if (typeof document !== "undefined") AFExplorer.attach(document, (...args) => fetch(...args));
