import json
import time
import logging
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.generic.websocket import WebsocketConsumer
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Membership, Site
from apps.notebooks.models import Notebook
from apps.workers.models import Worker
from apps.ws.tasks import task_start_websocket_worker
from apps.ws.utils import client_group, worker_group
from apps.storage.s3utils import clean_worker_files

from apps.ws.utils import get_client_server_url

log = logging.getLogger(__name__)


class ClientProxy(WebsocketConsumer):
    def connect(self):
        log.debug("Trying to connect client")

        self.notebook_id = int(self.scope["url_route"]["kwargs"]["notebook_id"])
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]

        self.user = self.scope["user"]

        nb = Notebook.objects.get(pk=self.notebook_id)
        if nb.hosted_on.share == Site.PRIVATE:
            if self.user.is_anonymous:
                self.close()
            else:
                member = Membership.objects.filter(user=self.user, host=nb.hosted_on)
                owner = nb.hosted_on.created_by == self.user
                if not member and not owner:
                    self.close()

        log.debug(f"Client connect to {self.notebook_id}/{self.session_id}")

        self.client_group = client_group(self.notebook_id, self.session_id)
        self.worker_group = worker_group(self.notebook_id, self.session_id)

        async_to_sync(self.channel_layer.group_add)(
            self.client_group, self.channel_name
        )

        self.server_address = None

        self.start_time = time.time()
        self.site_owner = nb.hosted_on.created_by

        self.accept()

    def disconnect(self, close_code):
        #
        # close worker
        #
        async_to_sync(self.channel_layer.group_send)(
            self.worker_group,
            {"type": "broadcast_message", "payload": {"purpose": "close-worker"}},
        )

        async_to_sync(self.channel_layer.group_discard)(
            self.client_group, self.channel_name
        )
        # log usage
        usage = time.time() - self.start_time
        prev_usage = json.loads(self.site_owner.profile.usage)

        if "usage" in prev_usage:
            prev_usage["usage"] += usage
        else:
            prev_usage["usage"] = usage

        self.site_owner.profile.usage = json.dumps(prev_usage)
        self.site_owner.profile.save()

    def receive(self, text_data):
        log.debug(f"Received from client: {text_data}")

        json_data = json.loads(text_data)

        if json_data.get("purpose", "") == "worker-ping":
            self.worker_ping()
        elif json_data.get("purpose", "") == "server-address":
            self.server_address = json_data.get("address")
            self.need_worker()
        elif json_data.get("purpose", "") == "run-notebook":
            async_to_sync(self.channel_layer.group_send)(
                self.worker_group,
                {"type": "broadcast_message", "payload": json_data},
            )
        elif json_data.get("purpose", "") in [
            "save-notebook",
            "display-notebook",
            "download-html",
            "download-pdf",
        ]:
            async_to_sync(self.channel_layer.group_send)(
                self.worker_group,
                {"type": "broadcast_message", "payload": json_data},
            )

    def broadcast_message(self, event):
        payload = event["payload"]
        self.send(text_data=json.dumps(payload))

    def need_worker(self):
        if self.server_address is None:
            return

        # usage = json.loads(self.site_owner.profile.usage).get("usage", 0)
        # log.debug(f"Current usage {usage} seconds")

        # async_to_sync(self.channel_layer.group_send)(
        #     self.client_group,
        #     {
        #         "type": "broadcast_message",
        #         "payload": {"purpose": "worker-state", "state": "UsageLimitReached"},
        #     },
        # )

        with transaction.atomic():
            log.debug("Create worker in db")
            worker = Worker(
                session_id=self.session_id,
                notebook_id=self.notebook_id,
                state="Queued",
            )
            if not self.user.is_anonymous:
                worker.run_by = self.user
            worker.save()
            job_params = {
                "notebook_id": self.notebook_id,
                "session_id": self.session_id,
                "worker_id": worker.id,
                "server_url": get_client_server_url(self.server_address),
            }
            transaction.on_commit(lambda: task_start_websocket_worker.delay(job_params))

    def worker_ping(self):
        workers = Worker.objects.filter(
            Q(state="Running") | Q(state="Queued") | Q(state="Busy"),
            session_id=self.session_id,
            notebook_id=self.notebook_id,
        )

        if not workers:
            self.need_worker()
        else:
            async_to_sync(self.channel_layer.group_send)(
                self.worker_group,
                {
                    "type": "broadcast_message",
                    "payload": {"purpose": "worker-ping"},
                },
            )

        if workers.filter(state="Queued"):
            async_to_sync(self.channel_layer.group_send)(
                self.client_group,
                {
                    "type": "broadcast_message",
                    "payload": {"purpose": "worker-state", "state": "Queued"},
                },
            )

        # clear stale workers
        workers = Worker.objects.filter(
            updated_at__lte=timezone.now()
            - timedelta(minutes=settings.WORKER_STALE_TIME)
        )
        # clean s3 data for worker
        for worker in workers:
            clean_worker_files(worker.notebook.hosted_on.id, worker.session_id)

        workers.delete()
