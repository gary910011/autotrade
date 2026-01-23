# config.py
# ============================================================
# Wi-Fi Throughput Automation - Global Configuration
# ============================================================

# Supported:
#   - "AP_TX"   : ASUS_AP sets CH/BW, DUT runs hostapd AP, DUT runs iperf (server or client per your design)
#   - "STA_TX"  : ASUS_AP sets CH/BW/RATE, DUT only connects as STA and runs iperf client
MODE = "STA_RX"

# ============================================================
# DUT / mssh
# ============================================================
DUT_HOST = "root@172.16.6.0"
MSSH_BIN = "mssh"

MSSH_TIMEOUT_SHORT = 10
MSSH_TIMEOUT_PING = 5
MSSH_TIMEOUT_STREAM = 0  # 0 = no timeout (stream)
MSSH_TIMEOUT_RATE = 15

# ============================================================
# DUT STA (STA_TX uses only these on DUT)
# ============================================================
STA_IFACE = "wlan0"
STA_IP = "192.168.50.101"
STA_WPA_CONF = "/var/wpa_supplicant.conf"

# ============================================================
# DUT AP (hostapd-based, for AP_TX only)
# ============================================================
AP_IFACE = "wlan1"
AP_IP = "192.168.10.100"
AP_NETMASK = "255.255.255.0"
AP_SUBNET_CIDR = "192.168.10.0/24"

HOSTAPD_CONF_20M = "/var/gm9k_cw80_test3.conf"
HOSTAPD_CONF_40M = "/var/hostapd_36_w40.conf"
HOSTAPD_CONF_80M = "/var/hostapd_36_w80.conf"
BW_TEMPLATE = {
    20: HOSTAPD_CONF_20M,
    40: HOSTAPD_CONF_40M,
    80: HOSTAPD_CONF_80M,
}

# ============================================================
# ASUS AP control (SSH)
# ============================================================
ASUS_AP_HOST = "192.168.50.1"
ASUS_AP_USER = "admin"
ASUS_AP_PASS = "garmin1234"
ASUS_AP_PORT = 65535       # 你原本用的 port
ASUS_AP_IFACE_5G = "eth7"  # 你原本用的 5G iface
ASUS_AP_APPLY_WAIT_SEC = 15

# =========================
# iPerf server (Linux PC)
# Shared by AP_TX / STA_TX / STA_RX
# =========================

IPERF_SERVER_AP_TX = "192.168.10.239"
IPERF_PORT_AP_TX = 5201

IPERF_SERVER_STA_TX = "192.168.50.239"
IPERF_PORT_STA_TX = 5201

IPERF_DURATION = 30
# STA_RX (reverse direction, same server as STA_TX)
IPERF_SERVER_STA_RX = IPERF_SERVER_STA_TX
IPERF_PORT_STA_RX = IPERF_PORT_STA_TX

# ============================================================
# Ping / connectivity check (from DUT to server)
# ============================================================
PING_RETRY = 15
PING_INTERVAL_SEC = 1.0

# ============================================================
# Test matrix
# ============================================================
TEST_BW_LIST = [20, 40, 80]
TEST_CHANNEL_LIST = [36, 149]

# BW20: MCS8 ~ 0 ; BW40/80: MCS9 ~ 0
TEST_MCS_TABLE = {
    20: list(range(8, -1, -1)),
    40: list(range(9, -1, -1)),
    80: list(range(9, -1, -1)),
}

# ============================================================
# Logging
# ============================================================
LOG_DIR = r"C:\Users\lindean\Desktop\Tput\tput_logs"

# ============================================================
# Excel report
# ============================================================

EXCEL_PATH = r"C:\Users\lindean\Desktop\Tput\Wi-Fi_Tput Test.xlsx"

# ASUS role-specific IPs
ASUS_AP_IP  = "192.168.50.1"   # ASUS acting as AP (STA_TX / STA_RX)
ASUS_STA_IP = "192.168.10.1"   # ASUS acting as STA (AP_TX / AP_RX)
