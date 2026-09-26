"""Actual SMTP wire submission to a loopback-only capture server."""

from __future__ import annotations

from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from socketserver import StreamRequestHandler, ThreadingTCPServer
from threading import Thread

from af.communications.contracts import (
    Channel,
    DeliveryState,
    NotificationIntent,
    Recipient,
    Route,
    Template,
)
from af.communications.delivery import MailManager, NotificationService
from af.communications.smtp import SMTPChannel, SMTPConfig
from af.communications.store import SQLiteDeliveryStore
from af.messaging.envelopes import PolicyContext


def test_real_local_smtp_submission_and_durable_duplicate_suppression(tmp_path) -> None:
    messages = []

    class Capture(StreamRequestHandler):
        def handle(self):
            self.wfile.write(b"220 localhost capture\r\n")
            while command := self.rfile.readline():
                verb = command.split(b" ", 1)[0].strip().upper()
                if verb in {b"EHLO", b"HELO"}:
                    self.wfile.write(b"250-localhost\r\n250 SIZE 2000000\r\n")
                elif verb == b"DATA":
                    self.wfile.write(b"354 Send data\r\n")
                    data = bytearray()
                    while (line := self.rfile.readline()) not in {b".\r\n", b""}:
                        data.extend(line[1:] if line.startswith(b"..") else line)
                    messages.append(BytesParser(policy=policy.default).parsebytes(bytes(data)))
                    self.wfile.write(b"250 Accepted\r\n")
                elif verb == b"QUIT":
                    self.wfile.write(b"221 Bye\r\n")
                    return
                else:
                    self.wfile.write(b"250 OK\r\n")

    server = ThreadingTCPServer(("127.0.0.1", 0), Capture)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    store = SQLiteDeliveryStore(tmp_path / "smtp.sqlite")
    try:
        config = SMTPConfig(
            "127.0.0.1", "sender@example.test", port=server.server_address[1], security="plain"
        )
        recipient = Recipient(
            "a",
            "fr",
            (Route(Channel.MAIL, "recipient@example.test", "smtp"),),
            frozenset({Channel.MAIL}),
        )
        service = NotificationService(
            channels=MailManager({"smtp": SMTPChannel(config)}, default="smtp"),
            store=store,
            authorize=lambda *_: True,
            recipients=lambda _: recipient,
            clock=lambda: datetime.now(UTC),
        )
        intent = NotificationIntent(
            "wire-test",
            {"fr": Template("Rapport prêt", "Bonjour Alex")},
            {},
            (Channel.MAIL,),
            PolicyContext("v1", "a" * 64, "notifications"),
            "wire-correlation",
            fallback_locale="fr",
        )
        job = service.plan(intent, (recipient,))[0]
        assert service.deliver(job).state is DeliveryState.ACCEPTED
        assert service.deliver(job).state is DeliveryState.ACCEPTED
        assert len(messages) == 1
        assert str(messages[0]["To"]) == "recipient@example.test"
        assert str(messages[0]["Subject"]) == "Rapport prêt"
        assert messages[0].get_content().strip() == "Bonjour Alex"
    finally:
        store.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
