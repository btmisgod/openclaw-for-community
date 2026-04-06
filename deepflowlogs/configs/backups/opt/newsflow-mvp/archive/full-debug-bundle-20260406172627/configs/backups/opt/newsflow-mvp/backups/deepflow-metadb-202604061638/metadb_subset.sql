-- MySQL dump 10.13  Distrib 8.0.31, for Linux (x86_64)
--
-- Host: mysql    Database: deepflow
-- ------------------------------------------------------
-- Server version	8.0.31

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `host_device`
--

DROP TABLE IF EXISTS `host_device`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `host_device` (
  `id` int NOT NULL AUTO_INCREMENT,
  `type` int DEFAULT NULL COMMENT '1.Server 3.Gateway 4.DFI',
  `state` int DEFAULT NULL COMMENT '0.Temp 1.Creating 2.Complete 3.Modifying 4.Exception',
  `name` varchar(256) DEFAULT '',
  `alias` char(64) DEFAULT '',
  `description` varchar(256) DEFAULT '',
  `ip` char(64) DEFAULT '',
  `hostname` char(64) DEFAULT '',
  `htype` int DEFAULT NULL COMMENT '1. Xen host 2. VMware host 3. KVM host 4. Public cloud host 5. Hyper-V',
  `create_method` int DEFAULT '0' COMMENT '0.learning 1.user_defined',
  `user_name` varchar(64) DEFAULT '',
  `user_passwd` varchar(64) DEFAULT '',
  `vcpu_num` int DEFAULT '0',
  `mem_total` int DEFAULT '0' COMMENT 'unit: M',
  `rack` varchar(64) DEFAULT NULL,
  `rackid` int DEFAULT NULL,
  `topped` int DEFAULT '0',
  `az` char(64) DEFAULT '',
  `region` char(64) DEFAULT '',
  `domain` char(64) NOT NULL DEFAULT '',
  `extra_info` text,
  `lcuuid` char(64) DEFAULT '',
  `synced_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`,`domain`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `host_device`
--

LOCK TABLES `host_device` WRITE;
/*!40000 ALTER TABLE `host_device` DISABLE KEYS */;
/*!40000 ALTER TABLE `host_device` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `ip_resource`
--

DROP TABLE IF EXISTS `ip_resource`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `ip_resource` (
  `id` int NOT NULL AUTO_INCREMENT,
  `ip` char(64) DEFAULT '',
  `alias` char(64) DEFAULT '',
  `netmask` int DEFAULT NULL,
  `gateway` char(64) DEFAULT '',
  `create_method` int DEFAULT '0' COMMENT '0.learning 1.user_defined',
  `userid` int DEFAULT '0',
  `isp` int DEFAULT NULL,
  `vifid` int DEFAULT '0',
  `vl2_net_id` int DEFAULT '0',
  `sub_domain` char(64) DEFAULT '',
  `domain` char(64) NOT NULL DEFAULT '',
  `region` char(64) DEFAULT '',
  `lcuuid` char(64) DEFAULT '',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`,`domain`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `ip_resource`
--

LOCK TABLES `ip_resource` WRITE;
/*!40000 ALTER TABLE `ip_resource` DISABLE KEYS */;
/*!40000 ALTER TABLE `ip_resource` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `vtap`
--

DROP TABLE IF EXISTS `vtap`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `vtap` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(256) NOT NULL,
  `raw_hostname` varchar(256) DEFAULT NULL,
  `state` int DEFAULT '1' COMMENT '0.not-connected 1.normal',
  `enable` int DEFAULT '1' COMMENT '0: stop 1: running',
  `type` int DEFAULT '0' COMMENT '1: process 2: vm 3: public cloud 4: analyzer 5: physical machine 6: dedicated physical machine 7: host pod 8: vm pod',
  `ctrl_ip` char(64) NOT NULL,
  `ctrl_mac` char(64) DEFAULT NULL,
  `tap_mac` char(64) DEFAULT NULL,
  `analyzer_ip` char(64) NOT NULL,
  `cur_analyzer_ip` char(64) NOT NULL,
  `controller_ip` char(64) NOT NULL,
  `cur_controller_ip` char(64) NOT NULL,
  `launch_server` char(64) NOT NULL,
  `launch_server_id` int DEFAULT NULL,
  `az` char(64) DEFAULT '',
  `region` char(64) DEFAULT '',
  `revision` varchar(256) DEFAULT NULL,
  `synced_controller_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `synced_analyzer_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `boot_time` int DEFAULT '0',
  `exceptions` int unsigned DEFAULT '0',
  `vtap_lcuuid` char(64) DEFAULT NULL,
  `vtap_group_lcuuid` char(64) DEFAULT NULL,
  `cpu_num` int DEFAULT '0' COMMENT 'logical number of cpu',
  `memory_size` bigint DEFAULT '0',
  `arch` varchar(256) DEFAULT NULL,
  `os` varchar(256) DEFAULT NULL,
  `kernel_version` varchar(256) DEFAULT NULL,
  `process_name` varchar(256) DEFAULT NULL,
  `current_k8s_image` varchar(512) DEFAULT NULL,
  `license_type` int DEFAULT NULL COMMENT '1: A类 2: B类 3: C类',
  `license_functions` char(64) DEFAULT NULL COMMENT 'separated by ,; 1: 流量分发 2: 网络监控 3: 应用监控',
  `tap_mode` int DEFAULT NULL,
  `team_id` int DEFAULT NULL,
  `expected_revision` text,
  `upgrade_package` text,
  `lcuuid` char(64) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `vtap`
--

LOCK TABLES `vtap` WRITE;
/*!40000 ALTER TABLE `vtap` DISABLE KEYS */;
/*!40000 ALTER TABLE `vtap` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `vtap_group_configuration`
--

DROP TABLE IF EXISTS `vtap_group_configuration`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `vtap_group_configuration` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT '1',
  `team_id` int DEFAULT '1',
  `max_collect_pps` int DEFAULT NULL,
  `max_npb_bps` bigint DEFAULT NULL COMMENT 'unit: bps',
  `max_cpus` int DEFAULT NULL,
  `max_millicpus` int DEFAULT NULL,
  `max_memory` int DEFAULT NULL COMMENT 'unit: M',
  `platform_sync_interval` int DEFAULT NULL,
  `sync_interval` int DEFAULT NULL,
  `stats_interval` int DEFAULT NULL,
  `rsyslog_enabled` tinyint(1) DEFAULT NULL COMMENT '0: disabled 1: enabled',
  `system_load_circuit_breaker_threshold` float(8,2) DEFAULT NULL,
  `system_load_circuit_breaker_recover` float(8,2) DEFAULT NULL,
  `system_load_circuit_breaker_metric` char(64) DEFAULT NULL,
  `max_tx_bandwidth` bigint DEFAULT NULL COMMENT 'unit: bps',
  `bandwidth_probe_interval` int DEFAULT NULL,
  `tap_interface_regex` text,
  `max_escape_seconds` int DEFAULT NULL,
  `mtu` int DEFAULT NULL,
  `output_vlan` int DEFAULT NULL,
  `collector_socket_type` char(64) DEFAULT NULL,
  `compressor_socket_type` char(64) DEFAULT NULL,
  `npb_socket_type` char(64) DEFAULT NULL,
  `npb_vlan_mode` int DEFAULT NULL,
  `collector_enabled` tinyint(1) DEFAULT NULL COMMENT '0: disabled 1: enabled',
  `vtap_flow_1s_enabled` tinyint(1) DEFAULT NULL COMMENT '0: disabled 1: enabled',
  `l4_log_tap_types` text COMMENT 'tap type info, separate by ","',
  `npb_dedup_enabled` tinyint(1) DEFAULT NULL COMMENT '0: disabled 1: enabled',
  `platform_enabled` tinyint(1) DEFAULT NULL COMMENT '0: disabled 1: enabled',
  `if_mac_source` int DEFAULT NULL COMMENT '0: 接口MAC 1: 接口名称 2: 虚拟机MAC解析',
  `vm_xml_path` text,
  `extra_netns_regex` text,
  `nat_ip_enabled` tinyint(1) DEFAULT NULL COMMENT '0: disabled 1: enabled',
  `capture_packet_size` int DEFAULT NULL,
  `inactive_server_port_enabled` tinyint(1) DEFAULT NULL COMMENT '0: disabled 1: enabled',
  `inactive_ip_enabled` tinyint(1) DEFAULT NULL COMMENT '0: disabled 1: enabled',
  `vtap_group_lcuuid` char(64) DEFAULT NULL,
  `log_threshold` int DEFAULT NULL,
  `log_level` char(64) DEFAULT NULL,
  `log_retention` int DEFAULT NULL,
  `http_log_proxy_client` char(64) DEFAULT NULL,
  `http_log_trace_id` text,
  `l7_log_packet_size` int DEFAULT NULL,
  `l4_log_collect_nps_threshold` int DEFAULT NULL,
  `l7_log_collect_nps_threshold` int DEFAULT NULL,
  `l7_metrics_enabled` tinyint(1) DEFAULT NULL COMMENT '0: disabled 1: enabled',
  `l7_log_store_tap_types` text COMMENT 'l7 log store tap types, separate by ","',
  `l4_log_ignore_tap_sides` text COMMENT 'separate by ","',
  `l7_log_ignore_tap_sides` text COMMENT 'separate by ","',
  `decap_type` text COMMENT 'separate by ","',
  `capture_socket_type` int DEFAULT NULL,
  `capture_bpf` varchar(512) DEFAULT NULL,
  `tap_mode` int DEFAULT NULL COMMENT '0: local 1: virtual mirror 2: physical mirror',
  `thread_threshold` int DEFAULT NULL,
  `process_threshold` int DEFAULT NULL,
  `ntp_enabled` tinyint(1) DEFAULT NULL COMMENT '0: disabled 1: enabled',
  `l4_performance_enabled` tinyint(1) DEFAULT NULL COMMENT '0: disabled 1: enabled',
  `pod_cluster_internal_ip` tinyint(1) DEFAULT NULL COMMENT '0: 所有集群 1: 采集器所在集群',
  `domains` text COMMENT 'domains info, separate by ","',
  `http_log_span_id` text,
  `http_log_x_request_id` char(64) DEFAULT NULL,
  `sys_free_memory_limit` int DEFAULT NULL COMMENT 'unit: %',
  `log_file_size` int DEFAULT NULL COMMENT 'unit: MB',
  `external_agent_http_proxy_enabled` tinyint(1) DEFAULT NULL COMMENT '0: disabled 1: enabled',
  `external_agent_http_proxy_port` int DEFAULT NULL,
  `proxy_controller_port` int DEFAULT NULL,
  `analyzer_port` int DEFAULT NULL,
  `proxy_controller_ip` varchar(128) DEFAULT NULL,
  `analyzer_ip` varchar(128) DEFAULT NULL,
  `wasm_plugins` text COMMENT 'wasm_plugin info, separate by ","',
  `so_plugins` text COMMENT 'so_plugin info, separate by ","',
  `yaml_config` text,
  `lcuuid` char(64) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `vtap_group_configuration`
--

LOCK TABLES `vtap_group_configuration` WRITE;
/*!40000 ALTER TABLE `vtap_group_configuration` DISABLE KEYS */;
/*!40000 ALTER TABLE `vtap_group_configuration` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-04-06  8:30:29
