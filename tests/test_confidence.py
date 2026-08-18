from app.discovery.confidence import rank_shortlist_by_confidence, score_site


def test_score_site():
    fields = {
        "elevation_m": {"value": 300.0, "status": "ok", "confidence": "high"},
        "slope_pct": {"value": 2.0, "status": "ok", "confidence": "high"},
        "flood_risk_score": {"value": 0.1, "status": "ok", "confidence": "medium"},
        "unsupported_field": {"value": None, "status": "absent", "confidence": "unknown"},
    }

    res = score_site(fields)
    assert 0.0 <= res["confidence_score"] <= 1.0
    assert len(res["field_breakdown"]) == 4
    assert len(res["weakest_fields"]) <= 4
    assert res["is_calibrated"] is False


def test_rank_shortlist_by_confidence():
    shortlist = [
        {
            "parcel_id": "PCL-LOW",
            "fields": {
                "f1": {"value": 1, "status": "failed", "confidence": "low"},
                "f2": {"value": 2, "status": "absent", "confidence": "unknown"},
            },
        },
        {
            "parcel_id": "PCL-HIGH",
            "fields": {
                "f1": {"value": 1, "status": "ok", "confidence": "high"},
                "f2": {"value": 2, "status": "ok", "confidence": "high"},
            },
        },
    ]

    ranked = rank_shortlist_by_confidence(shortlist)
    assert ranked[0]["parcel_id"] == "PCL-HIGH"
    assert ranked[1]["parcel_id"] == "PCL-LOW"
    assert ranked[0]["confidence_score"] > ranked[1]["confidence_score"]
