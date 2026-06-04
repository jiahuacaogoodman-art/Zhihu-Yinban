# -*- coding: utf-8 -*-
"""Regression tests for SPA-only / REST-only migration.

These tests intentionally assert that removed legacy entrypoints do not stay
reachable through accidental compatibility routes or service-worker cache lists.
"""

from pathlib import Path


LEGACY_EHR_ROUTES = [
    ("post", "/api/ehr/add"),
    ("get", "/api/ehr/list"),
    ("post", "/api/ehr/update"),
    ("post", "/api/ehr/delete"),
]


def test_legacy_ehr_routes_are_not_registered(client):
    """The admin SPA must use /api/ehr/patients instead of old action endpoints."""
    for method, path in LEGACY_EHR_ROUTES:
        response = getattr(client, method)(path, headers={"X-Auth-Token": "test-token"}, json={})
        assert response.status_code == 404, f"{method.upper()} {path} should be removed"


def test_service_worker_does_not_precache_legacy_admin_entrypoint():
    sw = Path("static/sw.js").read_text(encoding="utf-8")
    assets_block = sw.split("const STATIC_ASSETS = [", 1)[1].split("];", 1)[0]
    assert "'/static/index.html'" not in assets_block
    assert '"/static/index.html"' not in assets_block
    assert "'/legacy'" not in assets_block
    assert '"/legacy"' not in assets_block
    assert "LEGACY_ENTRYPOINTS" in sw
