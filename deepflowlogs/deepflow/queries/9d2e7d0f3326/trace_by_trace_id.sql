SELECT time, app_service, endpoint, trace_id, span_id, parent_span_id, attribute_values FROM l7_flow_log WHERE trace_id='' ORDER BY time DESC LIMIT 200
