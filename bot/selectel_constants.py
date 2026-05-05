IAM_BASE = "https://cloud.api.selcloud.ru/identity/v3"
BILLING_BASE = "https://api.selectel.ru"

TOKEN_MAX_AGE = 20 * 3600       # refresh IAM token before 24-hour expiry
ERROR_RETRY_SEC = 30            # wait after generic error
RATE_LIMIT_RETRY_SEC = 30       # wait after 429 / 503 / 529

DEFAULT_REGIONS = ["ru-1", "ru-2", "ru-3", "ru-7", "ru-9"]
DEFAULT_VM_NAME = "selectalized-vm"


def neutron_url(region: str) -> str:
    return f"https://{region}.cloud.api.selcloud.ru"


def nova_url(region: str) -> str:
    return f"https://{region}.cloud.api.selcloud.ru/v2.1"
