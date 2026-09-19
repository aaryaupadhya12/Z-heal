import os

from locust import HttpUser, task, between


class ZoneRLUser(HttpUser):
    host = os.environ.get("TARGET_HOST", "http://localhost:8000")

    wait_time = between(0.1, 0.5)

    @task
    def work(self):
        self.client.get("/work?size=1000")