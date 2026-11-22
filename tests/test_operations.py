"""Running it for real: password, daily limit, deleting audits, CSV export."""

import csv
import io
import json

import pytest

from app import db, vision
from app.config import get_settings
from factories import item, reply, shelf_image
from test_scenarios import GROCERY, run, stocked_grocery_reply, submit


# ---- password ---------------------------------------------------------------------------------


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(get_settings(), "access_password", "s3cret")


def test_no_password_set_means_open_access(client):
    assert client.get("/").status_code == 200


def test_every_page_asks_for_the_password_when_one_is_set(client, locked):
    for path in ("/", "/new", "/audits", "/api/stats", "/docs", "/static/app.js"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.headers["www-authenticate"].startswith("Basic")


def test_wrong_and_malformed_credentials_are_refused(client, locked):
    assert client.get("/", auth=("anyone", "wrong")).status_code == 401
    assert client.get("/", headers={"Authorization": "Basic !!!not-base64!!!"}).status_code == 401
    assert client.get("/", headers={"Authorization": "Bearer s3cret"}).status_code == 401
    assert client.get("/", auth=("anyone", "")).status_code == 401


def test_the_right_password_works_with_any_username_and_the_health_check_stays_open(client, locked):
    assert client.get("/", auth=("whoever", "s3cret")).status_code == 200
    assert client.get("/", auth=("", "s3cret")).status_code == 200
    assert client.get("/health").status_code == 200  # so a hosting service can still check it is alive


def test_audits_cannot_be_started_without_the_password(client, model, locked):
    response = client.post("/audits", files={"file": ("s.jpg", shelf_image(), "image/jpeg")},
                           data={"planogram": json.dumps(GROCERY), "category": "grocery"})
    assert response.status_code == 401 and model.calls == []
    assert client.get("/api/stats", auth=("u", "s3cret")).json()["total"] == 0


# ---- daily limit ------------------------------------------------------------------------------


def test_the_daily_limit_stops_the_third_audit(client, model, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_audits_per_day", 2)
    model.queue(stocked_grocery_reply(), stocked_grocery_reply())
    run(client), run(client)
    blocked = submit(client)
    assert blocked.status_code == 429 and "daily limit of 2 audits" in blocked.text
    assert len(model.calls) == 2 and client.get("/api/stats").json()["total"] == 2


def test_mistakes_do_not_use_up_the_allowance(client, model, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_audits_per_day", 1)
    bad = client.post("/audits", files={"file": ("s.jpg", shelf_image(), "image/jpeg")},
                      data={"planogram": "{oops", "category": "grocery"})
    gif = submit(client, image=shelf_image(fmt="GIF"))
    assert bad.status_code == 400 and gif.status_code == 400
    assert db.get_db().runs_today() == 0
    model.queue(stocked_grocery_reply())
    assert run(client)["status"] == "completed"  # the one allowed audit is still available


def test_a_rerun_and_the_stateless_endpoints_count_too(client, model, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_audits_per_day", 2)
    model.queue(vision.OpenRouterError("down"), stocked_grocery_reply())
    first = run(client)  # run 1 (fails)
    assert client.post(f"/audits/{first['id']}/retry", follow_redirects=False).status_code == 303  # run 2
    assert client.post(f"/audits/{first['id']}/retry", follow_redirects=False).status_code == 429  # over the limit
    detect = client.post("/detect", files={"file": ("s.jpg", shelf_image(), "image/jpeg")})
    assert detect.status_code == 429 and "daily limit" in detect.json()["detail"]


def test_the_allowance_starts_again_the_next_day(client, model, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_audits_per_day", 1)
    model.queue(stocked_grocery_reply(), stocked_grocery_reply())
    run(client)
    assert submit(client).status_code == 429
    monkeypatch.setattr("app.db._today", lambda: "2999-01-01")  # tomorrow
    assert run(client)["status"] == "completed"


# ---- deleting an audit --------------------------------------------------------------------------


def test_an_audit_can_be_deleted_with_its_photo_and_tasks(client, model):
    model.queue(reply((1, [item("chips", 3, "Chips bag")]), (2, [])))
    audit = run(client)
    assert audit["tasks"] and db.get_db().image_path(audit["id"]) is not None
    page = client.get(f"/audits/{audit['id']}").text
    assert f'action="/audits/{audit["id"]}/delete"' in page and "Delete" in page

    response = client.post(f"/audits/{audit['id']}/delete", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/audits"
    assert client.get(f"/audits/{audit['id']}").status_code == 404
    assert client.get(f"/audits/{audit['id']}/image").status_code == 404
    assert db.get_db().image_path(audit["id"]) is None and db.get_db().get_tasks(audit["id"]) == []
    assert client.get("/api/stats").json()["total"] == 0


def test_delete_errors_and_the_json_api(client, model):
    assert client.post("/audits/SS-MISSING0/delete").status_code == 404
    assert client.delete("/api/audits/SS-MISSING0").status_code == 404
    busy = db.get_db().create_audit(category="grocery", category_label="x", category_name="", planogram=GROCERY,
                                    image=b"x", mime="image/jpeg", width=1, height=1, warnings=[])
    assert client.delete(f"/api/audits/{busy}").status_code == 409  # still processing

    model.queue(stocked_grocery_reply())
    done = run(client)
    assert client.delete(f"/api/audits/{done['id']}").json() == {"deleted": True}
    assert client.get(f"/api/audits/{done['id']}").status_code == 404


# ---- CSV export ------------------------------------------------------------------------------------


def rows_of(response):
    assert response.content[:3] == b"\xef\xbb\xbf"  # a byte-order mark, so Excel reads it as UTF-8
    return list(csv.reader(io.StringIO(response.text.lstrip("﻿"))))


def test_a_scored_audit_downloads_as_a_csv_report(client, model):
    model.queue(reply((1, [item("chips", 3, "Chips bag"), item("cereal", 4, "Cereal box")]),
                      (2, [item("cookies", 5, "Biscuit pack"), item("noodles", 4, "Instant noodles")])))
    audit = run(client)
    response = client.get(f"/audits/{audit['id']}/export.csv")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert f'{audit["id"]}.csv' in response.headers["content-disposition"]

    rows = rows_of(response)
    assert rows[0] == ["section", "row", "product", "expected", "found", "status", "note"]
    assert ["comparison", "1", "Chips bag", "6", "3", "short", ""] in rows
    assert ["comparison", "1", "Cereal box", "4", "4", "in place", ""] in rows
    score_row = next(r for r in rows if r[2] == "Compliance score")
    assert score_row[5] == f"{audit['score']}%"
    assert any(r[0] == "task" and r[2] == "Restock 3 x Chips bag in row 1" and r[5] == "high" and r[6] == "open" for r in rows)
    assert f'/audits/{audit["id"]}/export.csv' in client.get(f"/audits/{audit['id']}").text


def test_csv_cannot_smuggle_spreadsheet_formulas(client, model):
    planogram = {"rows": [{"row": 1, "expected": [{"product": "=HYPERLINK(\"http://evil\",\"x\")", "facings": 2}]}]}
    model.queue(reply((1, [item("whatever", 2, "=HYPERLINK(\"http://evil\",\"x\")")])))
    audit = run(client, planogram=planogram)
    assert audit["status"] == "completed"
    cells = [cell for row in rows_of(client.get(f"/audits/{audit['id']}/export.csv")) for cell in row]
    assert not any(cell.startswith("=") for cell in cells)
    assert any(cell.startswith("'=HYPERLINK") for cell in cells)


def test_only_scored_audits_can_be_exported(client, model):
    model.queue(reply((1, []), (2, [])))
    held = run(client)
    assert client.get(f"/audits/{held['id']}/export.csv").status_code == 409
    assert client.get("/audits/SS-MISSING0/export.csv").status_code == 404
    assert "export.csv" not in client.get(f"/audits/{held['id']}").text
