SELECT app_service, endpoint, count() AS cnt FROM l7_flow_log WHERE trace_id='6f746a04b427ee61c1c6329e2d45056c' GROUP BY app_service, endpoint ORDER BY cnt DESC LIMIT 200
