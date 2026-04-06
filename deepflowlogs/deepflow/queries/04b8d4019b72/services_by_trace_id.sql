SELECT app_service, endpoint, count() AS cnt FROM l7_flow_log WHERE trace_id='3e807043a90d92931f723c9743338ccb' GROUP BY app_service, endpoint ORDER BY cnt DESC LIMIT 200
