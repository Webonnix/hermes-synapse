"""Demo data for the clients module.

Fills an empty install with a client book that exercises every branch the UI
has to render: all four currencies, one-time next to recurring, every billing
frequency, an unassigned client, a client with two services, and agent
connections in each interesting state (connected, error, paused, none).

Idempotent — it refuses to run against a non-empty `clients` table, so a
restart or a second invocation cannot double the book.

    python -m backend.clients_demo          # seed
    python -m backend.clients_demo --force  # seed even if clients exist
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List

from backend import client_billing, clients, currency

logger = logging.getLogger("hermes.clients_demo")

# §82 — the starting FX table. Seeded by currency.ensure_schema() already; this
# is here so a demo reset restores the documented numbers even if they were
# edited.
DEMO_RATES = {"BYN": "3.27", "KZT": "515.50", "CNY": "7.24"}


def _future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


DEMO_CLIENTS: List[Dict[str, Any]] = [
    {
        "name": "Midot Project", "type": "COMPANY", "projectName": "Веб-платформа",
        "description": "Разработка и поддержка основной платформы",
        "contact": {"name": "Иван Петров", "email": "ivan@midot.com",
                    "phone": "+375 29 123-45-67", "telegram": "@ivan_midot", "isPrimary": True},
        "service": {"serviceName": "Разработка", "title": "Разработка и поддержка",
                    "serviceType": "RECURRING", "amount": "750", "currency": "USD",
                    "frequency": "MONTHLY", "billingDay": 1, "nextBillingDate": _future(9)},
        "agentIndex": 0,
    },
    {
        "name": "ShopEasy", "type": "COMPANY", "projectName": "Интернет-магазин",
        "contact": {"name": "Анна Смирнова", "email": "anna@shopeasy.com",
                    "phone": "+375 29 987-65-43", "isPrimary": True},
        "service": {"serviceName": "Поддержка", "title": "Техническая поддержка",
                    "serviceType": "RECURRING", "amount": "800", "currency": "BYN",
                    "frequency": "MONTHLY", "billingDay": 1, "nextBillingDate": _future(9)},
        "agentIndex": 1,
    },
    {
        "name": "Landing Pro", "type": "COMPANY", "projectName": "Лендинг",
        "contact": {"name": "Сергей Кузнецов", "email": "sergey@landingpro.com",
                    "phone": "+375 29 555-12-34", "isPrimary": True},
        # No agent: a one-off setup job that nobody is delivering continuously.
        "service": {"serviceName": "Разовая настройка", "title": "Настройка и запуск",
                    "serviceType": "ONE_TIME", "amount": "450", "currency": "USD",
                    "frequency": "ONE_TIME"},
        "agentIndex": None,
    },
    {
        "name": "Data Analytics", "type": "COMPANY", "projectName": "Аналитика",
        "contact": {"name": "Мария Волкова", "email": "maria@data.com",
                    "phone": "+375 29 333-44-55", "isPrimary": True},
        "service": {"serviceName": "Аналитика", "title": "Анализ данных",
                    "serviceType": "RECURRING", "amount": "200000", "currency": "KZT",
                    "frequency": "MONTHLY", "billingDay": 1, "nextBillingDate": _future(9)},
        "agentIndex": 2,
    },
    {
        "name": "Branding Studio", "type": "COMPANY", "projectName": "Дизайн",
        "contact": {"name": "Дмитрий Соколов", "email": "dmitry@brand.com",
                    "phone": "+375 29 222-33-44", "isPrimary": True},
        "service": {"serviceName": "Дизайн", "title": "Фирменный стиль",
                    "serviceType": "ONE_TIME", "amount": "750", "currency": "USD",
                    "frequency": "ONE_TIME"},
        "agentIndex": None,
    },
    {
        "name": "Cloud Solutions", "type": "COMPANY", "projectName": "Облачная инфраструктура",
        "contact": {"name": "Елена Лебедева", "email": "elena@cloud.com",
                    "phone": "+375 29 111-22-33", "isPrimary": True},
        "service": {"serviceName": "Инфраструктура", "title": "Облачные решения",
                    "serviceType": "RECURRING", "amount": "1800", "currency": "USD",
                    "frequency": "MONTHLY", "billingDay": 1, "nextBillingDate": _future(9)},
        # Deliberately broken, so the ERROR badge has something to render.
        "agentIndex": 3, "agentError": ("PROVIDER_TIMEOUT", "Провайдер не отвечает: timeout 30s"),
    },
    {
        "name": "Mobile App", "type": "COMPANY", "projectName": "Мобильное приложение",
        "contact": {"name": "Алексей Морозов", "email": "alexey@mobile.com",
                    "phone": "+375 29 777-88-99", "isPrimary": True},
        "service": {"serviceName": "Разработка", "title": "Разработка приложения",
                    "serviceType": "ONE_TIME", "amount": "3200", "currency": "USD",
                    "frequency": "ONE_TIME"},
        "agentIndex": 4,
        # Partially paid: exercises the PARTIALLY_PAID badge and the settlement
        # arithmetic in one row.
        "invoice": {"amount": "3200", "currency": "USD", "status": "ISSUED",
                    "dueDate": _future(5), "payment": "1500"},
    },
    {
        "name": "Fitness Club", "type": "COMPANY", "projectName": "Фитнес-клуб",
        "contact": {"name": "Ольга Иванова", "email": "olga@fitness.com",
                    "phone": "+375 29 444-55-66", "isPrimary": True},
        "service": {"serviceName": "Поддержка", "title": "Техническая поддержка",
                    "serviceType": "RECURRING", "amount": "600", "currency": "BYN",
                    "frequency": "MONTHLY", "billingDay": 1, "nextBillingDate": _future(9)},
        "agentIndex": None,
        # Overdue: due date in the past with no payment against it.
        "invoice": {"amount": "600", "currency": "BYN", "status": "ISSUED",
                    "dueDate": (date.today() - timedelta(days=12)).isoformat()},
    },
    {
        "name": "Shenzhen Trade", "type": "COMPANY", "projectName": "Логистика",
        "description": "Автоматизация закупок и логистики",
        "contact": {"name": "Li Wei", "email": "li.wei@sztrade.cn",
                    "telegram": "@liwei_sz", "isPrimary": True},
        "service": {"serviceName": "Автоматизация", "title": "Автоматизация закупок",
                    "serviceType": "RECURRING", "amount": "12000", "currency": "CNY",
                    "frequency": "QUARTERLY", "billingDay": 15, "nextBillingDate": _future(25)},
        "agentIndex": 5,
    },
    {
        "name": "Алексей Титов", "type": "PERSON", "projectName": "Персональный ассистент",
        "contact": {"name": "Алексей Титов", "telegram": "@atitov", "isPrimary": True},
        "service": {"serviceName": "Ассистент", "title": "Персональный AI-ассистент",
                    "serviceType": "RECURRING", "amount": "1200", "currency": "USD",
                    "frequency": "YEARLY", "billingDay": 10, "nextBillingDate": _future(60)},
        "agentIndex": 6, "agentPaused": True,
    },
    {
        "name": "EduPlatform", "type": "COMPANY", "projectName": "Онлайн-школа",
        "contact": {"name": "Наталья Ким", "email": "n.kim@eduplatform.io",
                    "phone": "+7 701 555-00-11", "isPrimary": True},
        # Two services on one client — the table shows the primary one, the card
        # shows both.
        "service": {"serviceName": "Инфраструктура", "title": "Хостинг и мониторинг",
                    "serviceType": "RECURRING", "amount": "450000", "currency": "KZT",
                    "frequency": "SEMI_ANNUAL", "billingDay": 5, "nextBillingDate": _future(40)},
        "extraServices": [
            {"serviceName": "Поддержка", "title": "Поддержка преподавателей",
             "serviceType": "RECURRING", "amount": "300", "currency": "USD",
             "frequency": "MONTHLY", "billingDay": 5, "nextBillingDate": _future(16)},
        ],
        "agentIndex": 7,
    },
    {
        "name": "Legacy Corp", "type": "COMPANY", "projectName": "Архивный проект",
        "status": "COMPLETED",
        "contact": {"name": "Виктор Белов", "email": "v.belov@legacy.corp", "isPrimary": True},
        "service": {"serviceName": "Разработка", "title": "Завершённый проект",
                    "serviceType": "ONE_TIME", "amount": "5000", "currency": "USD",
                    "frequency": "ONE_TIME", "status": "COMPLETED"},
        "agentIndex": None,
        "invoice": {"amount": "5000", "currency": "USD", "status": "ISSUED", "payment": "5000"},
    },
]


def _available_agents(limit: int = 8) -> List[str]:
    """Links to whatever agents this install actually has. The demo never
    invents agents — an empty agent network simply yields clients without
    connections, which is a legitimate state the UI must handle anyway."""
    return [agent["id"] for agent in clients.list_agents_for_picker()[:limit]]


def seed(force: bool = False) -> Dict[str, Any]:
    clients._init_schema()
    client_billing._init_schema()

    existing = clients.count_clients()
    if existing["total"] and not force:
        logger.info("Demo seed skipped: %d clients already exist", existing["total"])
        return {"status": "skipped", "clients": existing["total"]}

    currency.update_settings(rates=DEMO_RATES, display_currency="USD", actor_id="demo-seed")

    agent_ids = _available_agents()
    created: List[str] = []

    for index, spec in enumerate(DEMO_CLIENTS):
        payload = {
            "name": spec["name"], "type": spec["type"], "projectName": spec.get("projectName", ""),
            "description": spec.get("description", ""), "status": spec.get("status", "ACTIVE"),
            "contact": spec["contact"], "service": spec["service"],
        }
        agent_slot = spec.get("agentIndex")
        if agent_slot is not None and agent_slot < len(agent_ids):
            payload["agent"] = {
                "agentId": agent_ids[agent_slot],
                "connectionType": "BOT" if agent_slot % 2 == 0 else "API",
                "channel": "telegram" if agent_slot % 2 == 0 else "internal",
            }
        client = clients.create_client(payload, actor_id="demo-seed")
        created.append(client["id"])

        for extra in spec.get("extraServices", []):
            clients.add_client_service(client["id"], extra, actor_id="demo-seed")

        connections = client.get("agentConnections") or []
        # A freshly linked demo agent has no activity history of its own, and
        # the status resolver would (correctly) call it OFFLINE. Demo data is
        # meant to depict a working install, so the healthy links get a
        # heartbeat; the ones scripted to be broken or paused deliberately
        # do not.
        if connections and not spec.get("agentError") and not spec.get("agentPaused"):
            clients.touch_connection(connections[0]["id"])
        if connections and spec.get("agentError"):
            code, message = spec["agentError"]
            clients.update_connection(
                connections[0]["id"], {"errorCode": code, "errorMessage": message},
                actor_id="demo-seed",
            )
        if connections and spec.get("agentPaused"):
            clients.update_connection(
                connections[0]["id"], {"desiredState": "PAUSED"}, actor_id="demo-seed"
            )

        invoice_spec = spec.get("invoice")
        if invoice_spec:
            service = (client.get("services") or [{}])[0]
            invoice = client_billing.create_invoice(
                client_id=client["id"], amount=invoice_spec["amount"],
                source_currency=invoice_spec["currency"],
                client_service_id=service.get("id"),
                due_date=invoice_spec.get("dueDate"), status=invoice_spec.get("status", "ISSUED"),
                comment="Демо-данные", actor_id="demo-seed",
            )
            if invoice_spec.get("payment"):
                client_billing.create_payment(
                    client_id=client["id"], amount=invoice_spec["payment"],
                    payment_currency=invoice_spec["currency"], invoice_id=invoice["id"],
                    payment_method="bank", comment="Демо-платёж", actor_id="demo-seed",
                )

    client_billing.sweep_overdue_invoices()
    logger.info("Demo seed created %d clients", len(created))
    return {"status": "ok", "clients": len(created), "ids": created}


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import sys
    from backend.database import init_db
    from backend.logging_config import configure_logging

    configure_logging()
    init_db()
    print(seed(force="--force" in sys.argv))
