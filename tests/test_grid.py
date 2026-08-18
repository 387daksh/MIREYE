from app.grid.ici import ICIEngine, haversine_distance_km


def test_haversine_distance():
    # Distance between ~Dallas and Fort Worth (~50km)
    d = haversine_distance_km(32.7767, -96.7970, 32.7555, -97.3308)
    assert 40.0 < d < 60.0


def test_substation_capacity_and_ici():
    ici = ICIEngine()
    sub, dist = ici.find_nearest_substation(32.8, -97.0)
    assert sub is not None
    assert dist > 0
    assert sub.projected_available_mw >= sub.firm_headroom_mw

    analysis = ici.analyze_interconnection(
        lat=32.8,
        lng=-97.0,
        target_capacity_mw=100.0,
        slope_pct=3.0,
        epa_wetlands_pct=0.02,
    )
    assert "substation" in analysis
    assert "feasibility" in analysis
    assert "recall_bounds" in analysis
    assert analysis["feasibility"]["row_assessment"]["row_rating"] == "clear"

    # Token compressed version for LLMs
    compressed = ici.compress_for_llm(analysis)
    assert "sub" in compressed
    assert "firm_mw" in compressed
    assert "row" in compressed
