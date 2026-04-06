SELECT app_service, endpoint, count() AS cnt FROM l7_flow_log WHERE trace_id='48e90a4b9806ed7e9e73e528f35b4994' GROUP BY app_service, endpoint ORDER BY cnt DESC LIMIT 200
