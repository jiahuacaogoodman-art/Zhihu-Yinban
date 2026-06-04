# -*- coding: utf-8 -*-


def test_operations_overview_returns_base_metrics(client):
    r = client.get('/api/operations/overview')
    assert r.status_code == 200
    data = r.json()

    for field in (
        'total_beds', 'occupied_beds', 'occupancy_rate', 'total_residents',
        'pending_admissions', 'open_incidents', 'pending_handovers', 'today_care_records'
    ):
        assert field in data

    assert isinstance(data['occupancy_rate'], (int, float))
