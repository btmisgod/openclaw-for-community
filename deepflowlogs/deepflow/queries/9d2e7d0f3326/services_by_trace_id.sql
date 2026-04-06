SELECT app_service, endpoint, count() AS cnt FROM l7_flow_log WHERE trace_id='' GROUP BY app_service, endpoint ORDER BY cnt DESC LIMIT 200
