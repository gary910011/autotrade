# config.py
# ============================================================
# Wi-Fi Throughput Automation - Global Configuration
# ============================================================

# Supported:
#   - "AP_TX"
#   - "AP_RX"
#   - "STA_TX"
#   - "STA_RX"
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
# Unified Network (IMPORTANT)
# ============================================================
# 不論 DUT / ASUS / AP / STA
# 所有控制與資料平面都固定使用 192.168.50.x
# ============================================================

CONTROL_SUBNET = "192.168.50.0/24"

# ============================================================
# DUT STA
# ============================================================
STA_IFACE = "wlan0"
STA_IP = "192.168.50.101"
STA_WPA_CONF = "/var/wpa_supplicant.conf"

# ============================================================
# DUT AP (hostapd-based)
# 注意：AP mode 也一樣使用 192.168.50.x
# ============================================================
AP_IFACE = "wlan1"
AP_IP = "192.168.50.100"
AP_NETMASK = "255.255.255.0"
AP_SUBNET_CIDR = CONTROL_SUBNET

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
ASUS_AP_PORT = 65535
ASUS_AP_IFACE_5G = "eth7"
ASUS_AP_IFACE_2G = "eth6"
ASUS_AP_APPLY_WAIT_SEC = 15

# ============================================================
# iPerf server (Linux PC)
# ============================================================
# 全模式共用同一台 server
# ============================================================

IPERF_SERVER = "192.168.50.239"
IPERF_PORT = 5201
IPERF_DURATION = 300  # 正式測試你可以改成 300

# 為了相容舊程式碼，保留原名稱（但值統一）
IPERF_SERVER_AP_TX = IPERF_SERVER
IPERF_PORT_AP_TX = IPERF_PORT

IPERF_SERVER_STA_TX = IPERF_SERVER
IPERF_PORT_STA_TX = IPERF_PORT

IPERF_SERVER_STA_RX = IPERF_SERVER
IPERF_PORT_STA_RX = IPERF_PORT

# ============================================================
# Ping / connectivity check
# ============================================================
PING_RETRY = 15
PING_INTERVAL_SEC = 1.0

# ============================================================
# Test matrix
# ============================================================
TEST_BW_LIST = [20, 40, 80]
TEST_CHANNEL_LIST = [36, 149]

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

# ============================================================
# Legacy compatibility (DO NOT USE IN NEW CODE)
# ============================================================
# 這兩個只為了避免舊 code import 爆掉
ASUS_AP_IP  = ASUS_AP_HOST
ASUS_STA_IP = ASUS_AP_HOST

ASUS_5G_IFACE = "eth7"

ASUS_5G_IFACE = ASUS_AP_IFACE_5G
ASUS_2G_IFACE = ASUS_AP_IFACE_2G
# =========================
# Band definition
# =========================
SUPPORTED_BANDS = ["5G", "2G"]

# =========================
# Test matrix per band
# =========================
BAND_CHANNELS = {
    "5G": [36, 149],
    "2G": [6],          # 先只開 CH6（你已指定）
}

BAND_BW = {
    "5G": [20, 40, 80],
    "2G": [20],
}

# STA rate plan（邏輯層會用）
BAND_STA_RATE_PLAN = {
    "5G": {
        "type": "5g_rate",
        "mcs": list(range(9, -1, -1)),
    },
    "2G": {
        "11n": list(range(15, 7, -1)),  # MCS15~8
        "11g": [54],
        "11b": [11],
    },
}

# =========================
# DUT AP hostapd conf
# =========================
HOSTAPD_CONF_2G_20M = "/var/gm9k_2p4G_test3.conf"
