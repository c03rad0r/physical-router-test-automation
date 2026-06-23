import os

BACKEND_PORT = 2121
CGI_PORT = 8080
PING_HOST = "1.1.1.1"

NOSTR_RELAYS = [
    r.strip() for r in os.environ.get("NOSTR_RELAYS", "wss://relay.cashu.email").split(",") if r.strip()
]
BLOSSOM_SERVERS = [
    s.strip() for s in os.environ.get("BLOSSOM_SERVERS", "https://blossom.psbt.me").split(",") if s.strip()
]

TEST_MINT_URL = os.environ.get("TOLLGATE_TEST_MINT_URL", "https://testnut.cashu.exchange")

# Go backend (gonuts) only supports Keyset ID V1 (00-prefix, 8-byte).
# CDK 0.16.0+ generates V2 (01-prefix, 33-byte). LOCAL_MINT_URL works
# with Python CLI but crashes the Go backend on startup.
LOCAL_MINT_URL = os.environ.get("TOLLGATE_LOCAL_MINT_URL", "http://10.99.99.1:8085")
LOCAL_502_MINT_URL = os.environ.get("TOLLGATE_LOCAL_502_MINT_URL", "http://10.99.99.1:8086")
_V2_DEFAULT = (
    "http://v2.testnut.cdk.lan:8383"
    if os.environ.get("TOLLGATE_VIRTUAL_LAB")
    else "https://testnut.cashu.space"
)
V2_MINT_URL = os.environ.get("TOLLGATE_V2_MINT_URL", _V2_DEFAULT)

TOKEN_MIN = 1
TOKEN_SMALL = 3
TOKEN_DEFAULT = 4
TOKEN_LONG = 10
DEFAULT_STEP_SIZE_MS = 5000
PRODUCTION_STEP_SIZE_MS = 5000

POC_GATEWAY = os.environ.get("TOLLGATE_VIRTUAL_GATEWAY", "10.99.99.1")
NDS_PORTAL_PORT = int(os.environ.get("TOLLGATE_NDS_PORTAL_PORT", "2050"))

ANDROID_CAPTIVE_PORTAL = "com.android.captiveportallogin"
ANDROID_CAPTIVE_PORTAL_ACTIVITY = "com.android.captiveportallogin/.CaptivePortalLoginActivity"
