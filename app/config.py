"""全局配置：读取 .env / 环境变量，无需额外依赖。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .messages_registry import KEYS
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析，不覆盖已存在的环境变量。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_list(key: str) -> list[str]:
    return [x.strip() for x in _env(key).split(",") if x.strip()]


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "on")


def _env_text(key: str, default: str = "") -> str:
    """读多行文案：.env 一行只能放一个配置，换行在 .env 里写成 \\n，读出来还原成真实换行。"""
    return _env(key, default).replace("\\n", "\n")


@dataclass
class Settings:
    # 飞书
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_encrypt_key: str = ""
    feishu_verification_token: str = ""
    feishu_chat_ids: list[str] = field(default_factory=list)

    # 运行
    database_url: str = "sqlite:///data/app.db"
    media_dir: str = "data/media"
    rules_file: str = "config/parse_rules.yaml"
    # 商品图片上传：单张大小上限（MB，0 = 不限）。
    # **为什么必须限**：走域名时 nginx 有 client_max_body_size 兜着，而现场常常是
    # 直连 8000 —— 那一层没有限制，手机原图（5~15MB）传一半断了就是一句说不清的
    # 「上传失败」；而且图是先整个读进内存再落盘，多张一起传会同时占几份内存
    # （2026-10-09 定：默认 5MB，够放运营用的商品图）
    upload_image_max_mb: int = 5
    # 公网部署时改为 https://你的域名（用于生成图片外链与回调地址提示）
    public_base_url: str = "http://127.0.0.1:8000"

    # 后台账号（存库）：首次启动自动建管理员账号
    # 账号取 ADMIN_USERNAME（留空用 admin），密码取 ADMIN_PASSWORD（留空用 xianyu2026）
    admin_username: str = ""
    admin_password: str = ""
    # 登录会话签名密钥，留空时自动生成并保存到 data/.secret_key
    secret_key: str = ""
    session_ttl_hours: int = 12
    # 为 1 时 /media（订单截图）免鉴权，方便供应链直接打开导出链接里的图片
    auth_public_media: bool = False

    # OCR
    ocr_provider: str = "none"          # none | tesseract | http
    ocr_http_url: str = ""
    ocr_http_field: str = "image"
    ocr_http_text_key: str = "text"
    ocr_lang: str = "chi_sim+eng"
    # OCR 前把图片灰度并放大的倍数（截图小字必需，1 表示只灰度不放大）
    ocr_upscale: int = 2
    # 从订单截图里裁出商品缩略图（依赖 tesseract 词框定位，裁剪不出就跳过）
    product_image_crop: bool = True
    # 订单截图按区域裁剪再识别（金额为主）：整图那些小字召回很差，裁出来放大
    # 单独认更准；只会纠正整图明显不对的值，认不出就维持原样
    ocr_roi: bool = True
    # 收货人姓名也走区域识别。实测（2026-09-29，12 张真实截图对照订单真值）：
    # 整图对了 5/12，ROI 纠正成功 0/12（「姜志雄」认成「委志雄」这种差一个字）——
    # 闲鱼对姓名那块做了处理，放大也救不回来。而姓名填错比留空更危险：
    # 留空会进「待核对」让运营补，填错则可能直接照着错的发出去。所以默认关。
    ocr_roi_name: bool = False

    # 聚合
    image_follow_window_seconds: int = 120

    # 只处理两类消息：订单模板消息 + 闲鱼订单截图，其余消息过滤不处理
    # 判定订单截图的关键词（OCR 文本里命中数达到 screenshot_min_hits 才归档）
    screenshot_keywords: list[str] = field(default_factory=list)
    screenshot_min_hits: int = 2
    # 处理完成后的回执通知：reply(群内回复该消息) | direct(私信发送人) | off
    notify_mode: str = "reply"
    # 闲鱼商品发布页地址：详情页「带 入闲鱼发布页」跳这里（闲鱼未开放卖家发布接口，发布动作由人完成）
    xianyu_publish_url: str = "https://www.goofish.com/publish"
    # 群里的通知照发之外，再私聊同步一条给发送人本人：
    # 群里消息多，@ 很容易被刷过去漏掉，本人盯着自己的私信就不会漏
    notify_pm_sync: bool = True
    # 回执里是否附带后台订单详情链接
    notify_with_link: bool = True
    # 模板消息没有配套订单截图时，回群提醒发送人补发（一条模板只提醒一次）
    notify_missing_image: bool = True
    # 各条消息的发送方式（群里 / 私信给谁），键见 messages_registry.KEYS；
    # 留空 = 用 registry 里记的默认方式（也就是这套功能上线前的老行为）
    message_routes: dict[str, str] = field(default_factory=dict)
    # 人工标记学到的解析策略（消息页「不是订单 / 其实是订单」）：关掉就只按规则文件判定
    parse_policy_enabled: bool = True

    # —— 回执推送策略（可在系统设置页配置）——
    # 允许下发的时段（本地时间，多段用逗号分隔，如 09:00-22:00；留空=全天可发）
    notify_active_hours: str = ""
    # 非下发时段的策略：hold=挂起，恢复后合并补发；drop=丢弃不补发
    notify_quiet_action: str = "hold"
    # 同一订单的最小回执间隔（秒）：防止同一订单被反复回执
    notify_min_interval_seconds: int = 600
    # 每小时最多下发条数（0=不限）
    notify_max_per_hour: int = 0
    # 挂起的回执恢复后是否合并成一条发送
    notify_digest: bool = True
    # 等订单截图补全订单号的时长（秒）：这段时间先压住回执，拿到正式订单号再推（0=不等待）
    notify_wait_screenshot_seconds: int = 60
    # 这条消息已经过去多少分钟后就不再下发回执（0=不限）：
    # 补录历史消息、或服务中断恢复时，别把几十条「已收到」一次性刷到群里
    notify_max_age_minutes: int = 60
    # 时段判断所用时区偏移（小时），默认东八区
    notify_utc_offset: int = 8

    # 后台自动同步（轮询拉取消息，未配置事件回调时的兜底方案）
    auto_sync: bool = False
    sync_interval_seconds: int = 300
    sync_lookback_minutes: int = 15
    # 新加入的同步群首次同步回补多少天（只回看 15 分钟的话，加群前的历史消息永远进不来）
    sync_backfill_days: int = 7

    # —— 语音播报（系统设置页可配置，浏览器 Web Speech 朗读）——
    # 1=开启；0=关闭（关闭后不再产生播报事件，页面也不朗读）
    voice_enabled: bool = True
    # 音量 0~1 / 语速 0.1~10 / 音调 0~2（浏览器 TTS 参数）
    voice_volume: float = 1.0
    voice_rate: float = 1.0
    voice_pitch: float = 1.0
    # 朗读语言，默认普通话
    voice_lang: str = "zh-CN"
    # 指定音色名称（留空=系统默认音色）
    voice_name: str = ""
    # 同一条播报重复朗读次数
    voice_repeat: int = 1
    # 播报文案：支持 {订单号} {产品} {供应商} {收货人} {物流单号} 占位符
    voice_order_text: str = "你有一笔订单要处理，请实际处理！"
    voice_tracking_text: str = "你有一笔订单供应商已发货，请及时完成店铺发货操作！"
    # 逆向退单播报
    voice_refund_text: str = "有退单订单请及时处理拦截！"
    # 自定义录音（自己录的 mp3/wav）：配了就播录音，没配才用上面的合成音。
    # 存的是 data/media/voice 下的文件名，三个播报场景各一条
    voice_clip_order: str = ""
    voice_clip_tracking: str = ""
    voice_clip_refund: str = ""
    # 系统自带的默认人声（随安装自带的三段提示音）：没自定义录音时用它，
    # 关掉则回落到旧的「浏览器合成音」策略
    voice_default_clip: bool = True

    # 群里补发的订单截图：晚到或没跟模板一起发的图，按收货人回填到缺截图的订单
    screenshot_backfill_enabled: bool = True
    # 只允许回填多久之内的订单（天）
    screenshot_backfill_days: int = 7

    # —— 渔货栈（学员和自营团队的选货平台，与渔枢系统同一个进程，页面挂在 /self 下）——
    # 关掉后 8000 就只有渔枢系统；渔枢系统连不上自己的库时也可以先关掉排障
    hub_enabled: bool = True
    # 渔枢系统自己的数据库（商品 / 订单 / 采购 / 结算），与渔枢系统分开存，互不影响。
    # 留空时回落到 DATABASE_URL（老的单端部署）
    hub_database_url: str = ""

    # —— 订单数据备份（每日定时归档，后台可查看/下载）——
    backup_enabled: bool = True
    backup_dir: str = "data/backups"
    # 每天几点跑（北京时间 24 小时制）
    backup_hour: int = 3
    # 备份保留天数，超期的自动删除
    backup_keep_days: int = 7

    # —— 每日出单汇总通知（按「人」统计当天出单 / 退单，定时推到群里）——
    daily_summary_enabled: bool = True
    # 每天几点推送（北京时间 24 小时制）：24 = 当天 24:00（即次日 00:00）推送当日汇总
    daily_summary_hour: int = 24
    # 推送目标群 chat_id（多个用英文逗号分隔）；留空依次回落到 FEISHU_CHAT_IDS、已加入的同步群
    daily_summary_chat_ids: list[str] = field(default_factory=list)
    # 汇总每行是否 @ 出单人（需要拿到该成员的 open_id）
    daily_summary_at_users: bool = True
    # 当天无人出单、也无退单时是否仍然推送
    daily_summary_skip_empty: bool = True

    # —— 渔货栈：库存为 0 自动下架 + 群通知（2026-10-11 用户拍板）——
    # 在架货源库存为 0 时自动从渔货栈下架（is_listed=False），并往飞书群发通知
    zero_stock_auto_unlist: bool = True
    # 巡检间隔（秒）；0 = 只在启动时跑一次
    zero_stock_interval_seconds: int = 600
    # 通知发到哪些群（留空依次回落到 DAILY_SUMMARY_CHAT_IDS、FEISHU_CHAT_IDS、已加入的同步群）
    stock_out_chat_ids: list[str] = field(default_factory=list)
    # 通知要 @ 的人（姓名逗号分隔）：open_id 现查群成员名单，查不到就只发文字不 @
    stock_out_cc_names: list[str] = field(default_factory=list)
    # 上面这些人的 open_id（与姓名一一对应，形如 ou_xxx）：配了就不查群成员名单
    stock_out_cc_open_ids: list[str] = field(default_factory=list)
    # 通知文案模板（占位符：{条数} {明细} {提示} {at}；留空用内置文案）
    stock_out_template: str = ""

    # —— 重复订单去重（群里重复发同一笔订单）——
    # 与多久之内的订单比对业务指纹（天）；0=只跟同订单号的比对
    order_dedupe_days: int = 30
    # 命中重复且内容无变化时，是否回群提醒「已收到，未变化」（不重复归档/不重复推单）
    dedupe_notify: bool = True

    # —— 逆向退单（群里第一行带「此单退 / 退单」等关键词）——
    # 退单关键词：模板消息看第一行（前 20 字），一句人话看整条消息
    refund_keywords: list[str] = field(default_factory=list)
    # 撤单关键词：群里发「撤单 / 取消订单」等，未发货的订单置为已撤单（只留档）
    cancel_keywords: list[str] = field(default_factory=list)
    # 退单消息回溯多少天去匹配原订单
    refund_match_days: int = 90
    # 退单 / 撤单消息回溯多少天（按发送人兜底关联订单用）
    intent_match_days: int = 7
    # 退单是否需要回群通知发送人「已识别退单，请及时处理拦截」
    refund_notify: bool = True
    # 退单回执里除 @ 发送人外，还要一起 @ 的固定人员（供应链对接人，如「牛磊」）：
    # 姓名逗号分隔；open_id 现查现用（群成员名单），查不到就只发文字不 @
    refund_cc_names: list[str] = field(default_factory=list)
    # 上面这些人的 open_id（与姓名一一对应，形如 ou_xxx）：配了就不查群成员名单
    refund_cc_open_ids: list[str] = field(default_factory=list)
    # 退单回执文案：@ 发送人的一句（确认收到）+ @ 固定人员的一句（处理口径）
    refund_ack_text: str = "退单信息已收到！"
    refund_cc_text: str = "已识别到本单退单申请，会尽快联系供应链拦截处理！有问题随时沟通。"
    # 撤单（群里发消息撤单 / 撤回消息）是否需要回群告知处理结果
    cancel_notify: bool = True
    # —— 通知消息模板（系统设置页可编辑）——
    # 收到模板消息 / 订单截图、订单归档后回群的通知模板（留空 = 用系统内置格式）
    notify_receipt_template: str = ""
    # 退单通知里「订单明细」那几行的模板（留空 = 用系统内置格式）
    notify_refund_detail_template: str = ""
    # 回执 @ 谁：sender=@发送人、names=@下面名单里的人（可多个，逗号分隔）
    notify_receipt_at: str = "sender"
    # 回执要额外 @ 的人（姓名逗号分隔，可多个：如「牛磊,小鱼」）：按姓名在群里查 open_id
    notify_receipt_at_names: list[str] = field(default_factory=list)
    # 上面这些人的 open_id（与姓名一一对应）：配了就不查群成员名单
    notify_receipt_at_open_ids: list[str] = field(default_factory=list)
    # 退单通知 @ 谁：sender=@发送人、cc=@供应链对接人（逗号分隔，可只选一个）
    notify_refund_at: str = "sender,cc"
    # —— 其余通知的模板（系统设置页按分类编辑，留空都用系统内置格式）——
    # 群里重复发了同一笔订单时的提醒
    notify_duplicate_template: str = ""
    # 撤单回执（还没发货就撤掉）
    notify_cancel_template: str = ""
    # 命中了退单 / 撤单意图但没落到订单上时的提示（変量：标题 / 说明 / 提示）
    notify_intent_template: str = ""
    # 缺订单截图时的提醒
    notify_missing_image_template: str = ""
    # 静默期结束后把多条回执并成一条的摘要（変量：条数 / 明细 / 更多）
    notify_digest_template: str = ""
    # 推单给供应商的复制文案
    push_template: str = ""
    # 供应商发货后同步物流单号到群里的文案
    tracking_template: str = ""
    # 每日出单汇总（默认 24 点发）的文案（変量：日期 / 明细 / 合计 / 人数 …）
    daily_summary_template: str = ""
    # 页面自动刷新：停在「数据总览」/「订单归档」时，多少秒自动拉一次最新数据（0 = 关闭）
    # 前端还有两道保护：页面不可见（切到别的标签）不请求、上一页加载慢会自动放慢
    auto_refresh_sec: int = 60
    # 截图重识别（兜底重试）：临时单号的订单隔多久重跑一次截图 OCR（0 = 关闭）
    ocr_reocr_minutes: int = 15
    # 同一张截图最多重试几次（超过就只提示，不再空转）
    ocr_max_attempts: int = 3
    # 重识别只回溯多少天内的订单
    ocr_reocr_days: int = 7

    # 菜鸟物流查询（LINK 网关）：订单详情里按运单号查轨迹，参数在系统设置页配
    cainiao_enabled: bool = False
    # 网关地址（正式环境 https://link.cainiao.com/gateway/link.do；沙箱 http://linkdaily.tbsandbox.com/gateway/link.do）
    cainiao_gateway_url: str = ""
    # 调用的 API 名（菜鸟叫 msg_type），如 CNTECH_LV_LOGISTICS_DETAIL_GET
    cainiao_msg_type: str = ""
    # APPKEY 对应的秘钥（参与签名）
    cainiao_app_secret: str = ""
    # 调用方的 CPCODE（菜鸟叫 logistic_provider_id）
    cainiao_cp_code: str = ""
    # 应用 code（菜鸟 LINK 网关一般不需要，留空就不发送）
    cainiao_app_code: str = ""
    # 报文结构：arg0 = {"arg0": {...}}，flat = 平铺 {"mailNo": ...}。
    # 两种说法都出现过（实测 500 vs 服务端堆栈说 S13 就是缺 arg0），系统按这个先发，
    # 没查到轨迹会自动换另一种重试，所以选错也不会查不了，只是多一次请求
    cainiao_interface_style: str = "arg0"
    cainiao_timeout_sec: int = 15

    # 物流轨迹本地存档（2026-10-13）：三端共用一份，避免同一单号被反复调菜鸟
    # 在途单：距上次查询超过这么多秒才再查一次（0 = 每次都实时查）
    logistics_trace_ttl_sec: int = 3600
    # 查不到轨迹的单（刚下单还没揽收）：空结果缓存短一些，别把额度刷在无效单上
    logistics_trace_empty_ttl_sec: int = 1800
    # 物流状态变化时飞书通知订单发送人（0 = 只记录不发送）
    logistics_notify_enabled: bool = True
    # ---- 时区口径（2026-10-13 用户拍板）：**全站统一按服务端时区显示** ----
    # 库里仍然存 UTC（内部一致，历史数据不用搬），展示一律按 display_tz 渲染，
    # **不跟浏览者的浏览器时区走** —— 以前页面时间会随 cookie 里的浏览器时区变，
    # 同一条数据两个人看到两个点，这就是「时间戳不是北京时间」的根因。
    # 真需要按各自时区看，把 TZ_FOLLOW_BROWSER 设成 1。
    display_tz: str = "Asia/Shanghai"
    tz_follow_browser: bool = False
    # 独立定时服务：每隔多少秒刷一批在途单（默认半小时）
    logistics_worker_interval_sec: int = 1800
    # 定时服务单轮最多查多少单（控制一轮的调用量）
    logistics_worker_batch: int = 20

    @property
    def backup_path(self) -> Path:
        p = Path(self.backup_dir)
        if not p.is_absolute():
            p = BASE_DIR / p
        return p

    @property
    def media_path(self) -> Path:
        p = Path(self.media_dir)
        if not p.is_absolute():
            p = BASE_DIR / p
        return p

    @property
    def rules_path(self) -> Path:
        p = Path(self.rules_file)
        if not p.is_absolute():
            p = BASE_DIR / p
        return p

    @property
    def feishu_enabled(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret)


settings = Settings(
    feishu_app_id=_env("FEISHU_APP_ID"),
    feishu_app_secret=_env("FEISHU_APP_SECRET"),
    feishu_encrypt_key=_env("FEISHU_ENCRYPT_KEY"),
    feishu_verification_token=_env("FEISHU_VERIFICATION_TOKEN"),
    feishu_chat_ids=_env_list("FEISHU_CHAT_IDS"),
    database_url=_env("DATABASE_URL", "sqlite:///data/app.db"),
    media_dir=_env("MEDIA_DIR", "data/media"),
    rules_file=_env("RULES_FILE", "config/parse_rules.yaml"),
    upload_image_max_mb=max(0, _env_int("UPLOAD_IMAGE_MAX_MB", 5)),
    public_base_url=_env("PUBLIC_BASE_URL", "http://127.0.0.1:8000"),
    admin_username=_env("ADMIN_USERNAME"),
    admin_password=_env("ADMIN_PASSWORD"),
    secret_key=_env("SECRET_KEY"),
    session_ttl_hours=_env_int("SESSION_TTL_HOURS", 12),
    auth_public_media=_env("AUTH_PUBLIC_MEDIA", "0").lower() in ("1", "true", "yes", "on"),
    ocr_provider=_env("OCR_PROVIDER", "none").lower(),
    ocr_http_url=_env("OCR_HTTP_URL"),
    ocr_http_field=_env("OCR_HTTP_FIELD", "image"),
    ocr_http_text_key=_env("OCR_HTTP_TEXT_KEY", "text"),
    ocr_lang=_env("OCR_LANG", "chi_sim+eng"),
    ocr_upscale=_env_int("OCR_UPSCALE", 2),
    product_image_crop=_env("PRODUCT_IMAGE_CROP", "1").lower() in ("1", "true", "yes", "on"),
    ocr_roi=_env("OCR_ROI", "1").lower() in ("1", "true", "yes", "on"),
    ocr_roi_name=_env("OCR_ROI_NAME", "0").lower() in ("1", "true", "yes", "on"),
    image_follow_window_seconds=_env_int("IMAGE_FOLLOW_WINDOW_SECONDS", 120),
    screenshot_keywords=(
        _env_list("SCREENSHOT_KEYWORDS")
        or ["订单编号", "订单号", "成交价", "实付", "商品总价", "运费", "买家", "收货", "闲鱼", "发货"]
    ),
    screenshot_min_hits=_env_int("SCREENSHOT_MIN_HITS", 2),
    notify_mode=_env("NOTIFY_MODE", "reply").lower(),
    xianyu_publish_url=_env("XIANYU_PUBLISH_URL", "https://www.goofish.com/publish"),
    notify_pm_sync=_env("NOTIFY_PM_SYNC", "1").lower() in ("1", "true", "yes", "on"),
    notify_with_link=_env("NOTIFY_WITH_LINK", "1").lower() in ("1", "true", "yes", "on"),
    notify_missing_image=_env("NOTIFY_MISSING_IMAGE", "1").lower() in ("1", "true", "yes", "on"),
    message_routes={key: _env(f"MSG_ROUTE_{key.upper()}", "") for key in KEYS},
    parse_policy_enabled=_env_bool("PARSE_POLICY_ENABLED", True),
    notify_active_hours=_env("NOTIFY_ACTIVE_HOURS"),
    notify_quiet_action=_env("NOTIFY_QUIET_ACTION", "hold").lower(),
    notify_min_interval_seconds=_env_int("NOTIFY_MIN_INTERVAL_SECONDS", 600),
    notify_max_per_hour=_env_int("NOTIFY_MAX_PER_HOUR", 0),
    notify_digest=_env("NOTIFY_DIGEST", "1").lower() in ("1", "true", "yes", "on"),
    notify_wait_screenshot_seconds=_env_int("NOTIFY_WAIT_SCREENSHOT_SECONDS", 60),
    notify_max_age_minutes=_env_int("NOTIFY_MAX_AGE_MINUTES", 60),
    notify_utc_offset=_env_int("NOTIFY_UTC_OFFSET", 8),
    auto_sync=_env("FEISHU_AUTO_SYNC", "0").lower() in ("1", "true", "yes", "on"),
    sync_interval_seconds=_env_int("SYNC_INTERVAL_SECONDS", 300),
    sync_lookback_minutes=_env_int("SYNC_LOOKBACK_MINUTES", 15),
    sync_backfill_days=_env_int("SYNC_BACKFILL_DAYS", 7),
    voice_enabled=_env_bool("VOICE_ENABLED", True),
    voice_volume=_env_float("VOICE_VOLUME", 1.0),
    voice_rate=_env_float("VOICE_RATE", 1.0),
    voice_pitch=_env_float("VOICE_PITCH", 1.0),
    voice_lang=_env("VOICE_LANG", "zh-CN"),
    voice_name=_env("VOICE_NAME"),
    voice_repeat=_env_int("VOICE_REPEAT", 1),
    voice_order_text=_env("VOICE_ORDER_TEXT", "你有一笔订单要处理，请实际处理！"),
    voice_tracking_text=_env(
        "VOICE_TRACKING_TEXT", "你有一笔订单供应商已发货，请及时完成店铺发货操作！"
    ),
    voice_refund_text=_env("VOICE_REFUND_TEXT", "有退单订单请及时处理拦截！"),
    voice_clip_order=_env("VOICE_CLIP_ORDER"),
    voice_clip_tracking=_env("VOICE_CLIP_TRACKING"),
    voice_clip_refund=_env("VOICE_CLIP_REFUND"),
    voice_default_clip=_env_bool("VOICE_DEFAULT_CLIP", True),
    screenshot_backfill_enabled=_env_bool("SCREENSHOT_BACKFILL_ENABLED", True),
    screenshot_backfill_days=_env_int("SCREENSHOT_BACKFILL_DAYS", 7),
    backup_enabled=_env_bool("BACKUP_ENABLED", True),
    hub_enabled=_env_bool("HUB_ENABLED", True),
    hub_database_url=_env("HUB_DATABASE_URL"),
    backup_dir=_env("BACKUP_DIR", "data/backups"),
    backup_hour=_env_int("BACKUP_HOUR", 3),
    backup_keep_days=_env_int("BACKUP_KEEP_DAYS", 7),
    daily_summary_enabled=_env_bool("DAILY_SUMMARY_ENABLED", True),
    daily_summary_hour=_env_int("DAILY_SUMMARY_HOUR", 24),
    daily_summary_chat_ids=_env_list("DAILY_SUMMARY_CHAT_IDS"),
    daily_summary_at_users=_env_bool("DAILY_SUMMARY_AT_USERS", True),
    daily_summary_skip_empty=_env_bool("DAILY_SUMMARY_SKIP_EMPTY", True),
    zero_stock_auto_unlist=_env_bool("ZERO_STOCK_AUTO_UNLIST", True),
    zero_stock_interval_seconds=max(0, _env_int("ZERO_STOCK_INTERVAL_SECONDS", 600)),
    stock_out_chat_ids=_env_list("STOCK_OUT_CHAT_IDS"),
    stock_out_cc_names=_env_list("STOCK_OUT_CC_NAMES") or ["牛磊", "胡子萍"],
    stock_out_cc_open_ids=_env_list("STOCK_OUT_CC_OPEN_IDS"),
    stock_out_template=_env_text("STOCK_OUT_TEMPLATE", ""),
    order_dedupe_days=_env_int("ORDER_DEDUPE_DAYS", 30),
    dedupe_notify=_env_bool("DEDUPE_NOTIFY", True),
    refund_keywords=(
        _env_list("REFUND_KEYWORDS")
        or ["此单退", "退单", "退货", "退款", "拦截"]
    ),
    cancel_keywords=(
        _env_list("CANCEL_KEYWORDS")
        or ["撤单", "取消订单", "此单取消", "作废", "不发了"]
    ),
    refund_match_days=_env_int("REFUND_MATCH_DAYS", 90),
    intent_match_days=_env_int("INTENT_MATCH_DAYS", 7),
    refund_notify=_env_bool("REFUND_NOTIFY", True),
    refund_cc_names=_env_list("REFUND_CC_NAMES") or ["牛磊"],
    refund_cc_open_ids=_env_list("REFUND_CC_OPEN_IDS"),
    refund_ack_text=_env("REFUND_ACK_TEXT", "退单信息已收到！"),
    refund_cc_text=_env(
        "REFUND_CC_TEXT", "已识别到本单退单申请，会尽快联系供应链拦截处理！有问题随时沟通。"
    ),
    cancel_notify=_env_bool("CANCEL_NOTIFY", True),
    notify_receipt_template=_env_text("NOTIFY_RECEIPT_TEMPLATE", ""),
    notify_refund_detail_template=_env_text("NOTIFY_REFUND_DETAIL_TEMPLATE", ""),
    notify_receipt_at=_env("NOTIFY_RECEIPT_AT", "sender").lower(),
    notify_receipt_at_names=_env_list("NOTIFY_RECEIPT_AT_NAMES"),
    notify_receipt_at_open_ids=_env_list("NOTIFY_RECEIPT_AT_OPEN_IDS"),
    notify_refund_at=_env("NOTIFY_REFUND_AT", "sender,cc").lower(),
    notify_duplicate_template=_env_text("NOTIFY_DUPLICATE_TEMPLATE", ""),
    notify_cancel_template=_env_text("NOTIFY_CANCEL_TEMPLATE", ""),
    notify_intent_template=_env_text("NOTIFY_INTENT_TEMPLATE", ""),
    notify_missing_image_template=_env_text("NOTIFY_MISSING_IMAGE_TEMPLATE", ""),
    notify_digest_template=_env_text("NOTIFY_DIGEST_TEMPLATE", ""),
    push_template=_env_text("PUSH_TEMPLATE", ""),
    tracking_template=_env_text("TRACKING_TEMPLATE", ""),
    daily_summary_template=_env_text("DAILY_SUMMARY_TEMPLATE", ""),
    auto_refresh_sec=max(0, _env_int("AUTO_REFRESH_SEC", 60)),
    ocr_reocr_minutes=max(0, _env_int("OCR_REOCR_MINUTES", 15)),
    ocr_max_attempts=max(1, _env_int("OCR_MAX_ATTEMPTS", 3)),
    ocr_reocr_days=max(1, _env_int("OCR_REOCR_DAYS", 7)),
    cainiao_enabled=_env_bool("CAINIAO_ENABLED", False),
    cainiao_gateway_url=_env(
        "CAINIAO_GATEWAY_URL", "https://link.cainiao.com/gateway/link.do"
    ),
    cainiao_msg_type=_env("CAINIAO_MSG_TYPE", "CNTECH_LV_LOGISTICS_DETAIL_GET"),
    cainiao_app_secret=_env("CAINIAO_APP_SECRET", ""),
    cainiao_cp_code=_env("CAINIAO_CP_CODE", ""),
    cainiao_app_code=_env("CAINIAO_APP_CODE", ""),
    cainiao_timeout_sec=max(3, _env_int("CAINIAO_TIMEOUT_SEC", 15)),
    cainiao_interface_style=(
        _env("CAINIAO_INTERFACE_STYLE", "arg0").strip().lower() or "arg0"
    ),
    logistics_trace_ttl_sec=max(0, _env_int("LOGISTICS_TRACE_TTL_SEC", 3600)),
    logistics_trace_empty_ttl_sec=max(
        0, _env_int("LOGISTICS_TRACE_EMPTY_TTL_SEC", 1800)
    ),
    logistics_notify_enabled=(
        _env("LOGISTICS_NOTIFY_ENABLED", "1").strip().lower()
        not in ("0", "false", "no", "off")
    ),
    display_tz=(_env("DISPLAY_TZ", "Asia/Shanghai").strip() or "Asia/Shanghai"),
    tz_follow_browser=(
        _env("TZ_FOLLOW_BROWSER", "0").strip().lower() in ("1", "true", "yes", "on")
    ),
    logistics_worker_interval_sec=max(
        60, _env_int("LOGISTICS_WORKER_INTERVAL_SEC", 1800)
    ),
    logistics_worker_batch=max(1, _env_int("LOGISTICS_WORKER_BATCH", 20)),
)

# 启动时确保目录存在
settings.media_path.mkdir(parents=True, exist_ok=True)
(BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
