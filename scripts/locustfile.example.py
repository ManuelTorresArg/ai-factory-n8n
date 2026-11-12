"""Plantilla de locustfile para tu repo: copiala a loadtest/locustfile.py.

La AI Factory la corre en headless contra la app levantada en el worktree del run
(factory.yaml > loadtest). El Builder agrega un @task por cada endpoint nuevo.
"""
from locust import HttpUser, between, task


class ApiUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task(5)
    def health(self):
        self.client.get("/", name="GET /")

    # @task(2)
    # def listar_items(self):
    #     self.client.get("/api/v1/items?limit=20", name="GET /api/v1/items")

    # @task(1)
    # def crear_item(self):   # escritura: datos descartables, nunca producción
    #     self.client.post("/api/v1/items", json={"name": "loadtest"}, name="POST /api/v1/items")
