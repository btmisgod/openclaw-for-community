SELECT app_service, endpoint, count() AS cnt FROM l7_flow_log WHERE trace_id='5d02c933a339cb3110a6d79faef58ad5' GROUP BY app_service, endpoint ORDER BY cnt DESC LIMIT 200
