"""商品类目树 / 规格模板 / 物流售后选项。

用途：
- Web「发布商品」表单的级联类目、规格快捷模板
- 后续渠道（闲鱼 / 抖店）类目映射的映射源

注意：与 1688 后台类目（`SupplierItem.category_id` 里的 cat_xxx）解耦——
那边是供货商侧类目，这里是本站自营商品的前台类目。
"""
from __future__ import annotations

import logging
import time
from functools import lru_cache

logger = logging.getLogger(__name__)

# ---- 两级类目树（一级 → 二级）----
CATEGORY_TREE: list[dict] = [
    {
        "id": "digital",
        "name": "数码家电",
        "children": [
            {"id": "digital_pc", "name": "电脑外设"},
            {"id": "digital_audio", "name": "耳机音响"},
            {"id": "digital_phone", "name": "手机配件"},
            {"id": "digital_smart", "name": "智能设备"},
            {"id": "digital_home", "name": "生活电器"},
        ],
    },
    {
        "id": "home",
        "name": "家居生活",
        "children": [
            {"id": "home_bed", "name": "床上用品"},
            {"id": "home_kitchen", "name": "厨房餐具"},
            {"id": "home_daily", "name": "日用百货"},
            {"id": "home_storage", "name": "收纳整理"},
        ],
    },
    {
        "id": "wear",
        "name": "服饰箱包",
        "children": [
            {"id": "wear_men", "name": "男装"},
            {"id": "wear_women", "name": "女装"},
            {"id": "wear_shoes", "name": "鞋靴"},
            {"id": "wear_bag", "name": "箱包配饰"},
        ],
    },
    {
        "id": "beauty",
        "name": "美妆个护",
        "children": [
            {"id": "beauty_skin", "name": "护肤"},
            {"id": "beauty_makeup", "name": "彩妆"},
            {"id": "beauty_hair", "name": "洗护"},
        ],
    },
    {
        "id": "food",
        "name": "食品生鲜",
        "children": [
            {"id": "food_snack", "name": "休闲零食"},
            {"id": "food_tea", "name": "茶饮冲调"},
            {"id": "food_fresh", "name": "生鲜果蔬"},
        ],
    },
    {
        "id": "sport",
        "name": "运动户外",
        "children": [
            {"id": "sport_fitness", "name": "健身器材"},
            {"id": "sport_outdoor", "name": "户外装备"},
        ],
    },
    {
        "id": "baby",
        "name": "母婴玩具",
        "children": [
            {"id": "baby_toy", "name": "玩具乐器"},
            {"id": "baby_care", "name": "母婴用品"},
        ],
    },
    {
        "id": "tool",
        "name": "五金设备",
        "children": [
            {"id": "tool_hardware", "name": "五金工具"},
            {"id": "tool_device", "name": "机电设备"},
        ],
    },
]

# 二级类目 id → 中文名（模板 / 面包屑用）
CATEGORY_NAME: dict[str, str] = {
    child["id"]: child["name"]
    for node in CATEGORY_TREE
    for child in node["children"]
}
CATEGORY_NAME.update({node["id"]: node["name"] for node in CATEGORY_TREE})

# 完整路径：「数码家电 / 电脑外设」
CATEGORY_PATH: dict[str, str] = {}
for _node in CATEGORY_TREE:
    for _child in _node["children"]:
        CATEGORY_PATH[_child["id"]] = f"{_node['name']} / {_child['name']}"


# =====================================================================
# 货源广场类目（对齐 1688 货源市场的两级类目 + 视觉素材）
# =====================================================================
# 结构与 1688「Selloffer」货源市场一致：
#   一级类目（左侧类目栏） → 二级类目（推荐类目磁贴 / 精选货源 Tab）
# 字段说明：
#   id       一级类目 id
#   name     一级类目名（类目栏主标签）
#   hint     一级类目副标签（1688 类目栏同行的第二组关键词）
#   icon     Bootstrap Icons 名（类目栏图标素材）
#   accent   类目色（卡片 / 磁贴底色，见 base.html 的 .accent-* 变量）
#   children 二级类目：id / name / icon / keywords（关键词用于把商品标题归入类目）
SOURCING_CATEGORY_TREE: list[dict] = [
    {
        "id": "apparel",
        "name": "女装男装",
        "hint": "服饰配饰",
        "icon": "bi-bag-heart",
        "accent": "rose",
        "children": [
            {"id": "ap_women", "name": "女装", "icon": "bi-person-standing-dress",
             "keywords": ("女装", "女式", "连衣裙", "裙", "雪纺", "针织衫", "打底", "外套")},
            {"id": "ap_men", "name": "男装", "icon": "bi-person-badge",
             "keywords": ("男装", "男式", "衬衫", "t恤", "T恤", "卫衣", "夹克", "羽绒服", "裤")},
            {"id": "ap_homewear", "name": "睡衣/家居服", "icon": "bi-moon-stars",
             "keywords": ("睡衣", "家居服", "浴袍", "内衣", "袜子", "袜")},
            {"id": "ap_acc", "name": "服饰配件", "icon": "bi-bandaid",
             "keywords": ("围巾", "丝巾", "腰带", "腰链", "手套", "帽子", "头巾", "领带")},
            {"id": "ap_jewelry", "name": "饰品配件", "icon": "bi-gem",
             "keywords": ("手饰", "饰品", "发饰", "头饰", "项饰", "耳饰", "手链", "项链", "戒指", "腕表", "手表")},
            {"id": "ap_bag", "name": "箱包皮具", "icon": "bi-handbag",
             "keywords": ("包", "背包", "钱包", "行李箱", "皮具", "卡包")},
            {"id": "ap_shoes", "name": "鞋靴", "icon": "bi-bag",
             "keywords": ("鞋", "靴", "拖鞋", "运动鞋", "帆布鞋")},
        ],
    },
    {
        "id": "office",
        "name": "办公文化",
        "hint": "运动户外",
        "icon": "bi-pencil-square",
        "accent": "slate",
        "children": [
            {"id": "of_stationery", "name": "学习文具", "icon": "bi-pencil",
             "keywords": ("笔", "文具", "笔记本", "本子", "尺", "橡皮", "文具盒", "试卷")},
            {"id": "of_paper", "name": "办公用纸", "icon": "bi-file-earmark-text",
             "keywords": ("纸", "打印", "标签", "便签", "票据")},
            {"id": "of_device", "name": "办公设备", "icon": "bi-printer",
             "keywords": ("打印", "碎纸", "装订", "办公椅", "白板", "计算器")},
            {"id": "of_book", "name": "图书文娱", "icon": "bi-book",
             "keywords": ("图书", "书籍", "音像", "手办", "拼图", "桌面游戏")},
            {"id": "of_sport", "name": "运动健身", "icon": "bi-bicycle",
             "keywords": ("瑜伽", "健身", "跑步", "哑铃", "拉力", "跳绳", "球")},
            {"id": "of_outdoor", "name": "户外装备", "icon": "bi-tree",
             "keywords": ("户外", "帐篷", "露营", "登山", "骑行", "钓")},
            {"id": "of_gift", "name": "礼品定制", "icon": "bi-gift",
             "keywords": ("礼品", "定制", "钥匙扣", "摆件", "纪念品")},
        ],
    },
    {
        "id": "tableware",
        "name": "餐饮饮具",
        "hint": "收纳清洁",
        "icon": "bi-cup-hot",
        "accent": "orange",
        "children": [
            {"id": "tw_cup", "name": "杯壶水具", "icon": "bi-cup-straw",
             "keywords": ("保温杯", "杯", "水壶", "壶", "瓶", "吸管")},
            {"id": "tw_kitchen", "name": "厨房工具", "icon": "bi-egg-fried",
             "keywords": ("厨房", "锅", "煲", "刀", "铲", "勺", "筷", "砧板", "烘焙")},
            {"id": "tw_tableware", "name": "餐具", "icon": "bi-basket",
             "keywords": ("餐具", "碗", "盘", "碟", "餐垫", "便当", "饭盒")},
            {"id": "tw_storage", "name": "整理用具", "icon": "bi-archive",
             "keywords": ("收纳", "整理", "置物", "挂架", "储物", "箱", "篮")},
            {"id": "tw_clean", "name": "清洁用品", "icon": "bi-brush",
             "keywords": ("清洁", "拖把", "抹布", "洗洁", "刷", "垃圾袋", "扫")},
            {"id": "tw_laundry", "name": "洗晒用品", "icon": "bi-wind",
             "keywords": ("衣架", "晾晒", "洗衣", "晒", "熨")},
            {"id": "tw_kitchen_small", "name": "厨房小电", "icon": "bi-lightning-charge",
             "keywords": ("电饭", "电煮", "破壁", "榨汁", "空气炸", "电热")},
        ],
    },
    {
        "id": "digital",
        "name": "家用电器",
        "hint": "数码电脑",
        "icon": "bi-cpu",
        "accent": "blue",
        "children": [
            {"id": "dg_pc", "name": "电脑外设", "icon": "bi-keyboard",
             "keywords": ("键盘", "鼠标", "显示器", "键鼠", "机箱", "硬盘", "内存", "U盘", "u盘")},
            {"id": "dg_phone_acc", "name": "手机配件", "icon": "bi-phone",
             "keywords": ("手机壳", "钢化膜", "手机支架", "数据线", "充电器", "充电宝", "防尘塞")},
            {"id": "dg_audio", "name": "耳机音响", "icon": "bi-headphones",
             "keywords": ("耳机", "音响", "音箱", "麦克风", "降噪", "耳麦")},
            {"id": "dg_charge", "name": "手机充电设备", "icon": "bi-lightning",
             "keywords": ("充电", "快充", "无线充", "typec", "Type-C", "usb")},
            {"id": "dg_smart", "name": "智能设备", "icon": "bi-cpu",
             "keywords": ("智能", "摄像头", "路由", "投影", "手环", "手表")},
            {"id": "dg_home_app", "name": "生活电器", "icon": "bi-plug",
             "keywords": ("风扇", "加湿", "取暖", "吸尘", "净水", "空气")},
            {"id": "dg_wire", "name": "线缆配件", "icon": "bi-ethernet",
             "keywords": ("线", "转换", "拓展坞", "转接", "排插", "插座")},
        ],
    },
    {
        "id": "hardware",
        "name": "五金工具",
        "hint": "家装建材",
        "icon": "bi-tools",
        "accent": "amber",
        "children": [
            {"id": "hw_tools", "name": "五金工具", "icon": "bi-wrench-adjustable",
             "keywords": ("工具", "扳手", "螺丝", "钳", "锤", "套筒")},
            {"id": "hw_electric", "name": "电工电气", "icon": "bi-lightbulb",
             "keywords": ("电工", "电线", "开关", "灯", "插座", "电笔")},
            {"id": "hw_machine", "name": "机电设备", "icon": "bi-gear",
             "keywords": ("设备", "电机", "泵", "机", "焊", "切割")},
            {"id": "hw_build", "name": "建筑装修", "icon": "bi-bricks",
             "keywords": ("建材", "瓷砖", "水泥", "涂料", "防水", "板材")},
            {"id": "hw_lock", "name": "锁具门配", "icon": "bi-lock",
             "keywords": ("锁", "合页", "门", "把手", "闭门器")},
            {"id": "hw_measure", "name": "量具仪表", "icon": "bi-rulers",
             "keywords": ("量具", "仪表", "卷尺", "水平仪", "万用表")},
        ],
    },
    {
        "id": "home_textile",
        "name": "家纺家饰",
        "hint": "箱包皮具",
        "icon": "bi-house-heart",
        "accent": "teal",
        "children": [
            {"id": "ht_bed", "name": "床上用品", "icon": "bi-moon",
             "keywords": ("床", "席", "枕", "被", "四件套", "床单", "床垫", "凉席")},
            {"id": "ht_decor", "name": "家居饰品", "icon": "bi-flower1",
             "keywords": ("饰", "摆件", "画", "相框", "花瓶", "香薰")},
            {"id": "ht_curtain", "name": "窗帘布艺", "icon": "bi-columns-gap",
             "keywords": ("窗帘", "布艺", "地毯", "地垫", "桌布")},
            {"id": "ht_living", "name": "客厅家具", "icon": "bi-lamp",
             "keywords": ("沙发", "茶几", "电视柜", "椅", "桌", "柜")},
            {"id": "ht_bath", "name": "卫浴用品", "icon": "bi-droplet-half",
             "keywords": ("浴", "毛巾", "牙刷", "马桶", "浴室", "花洒")},
            {"id": "ht_lighting", "name": "灯具照明", "icon": "bi-lamp-fill",
             "keywords": ("灯", "照明", "台灯", "灯带")},
        ],
    },
    {
        "id": "personal_care",
        "name": "个护家清",
        "hint": "美容护肤",
        "icon": "bi-droplet",
        "accent": "pink",
        "children": [
            {"id": "pc_skin", "name": "美容护肤", "icon": "bi-stars",
             "keywords": ("护肤", "洗面奶", "面膜", "精华", "面霜", "水乳", "防晒")},
            {"id": "pc_makeup", "name": "彩妆香氛", "icon": "bi-palette",
             "keywords": ("彩妆", "口红", "眼影", "粉底", "香水", "美妆", "卸妆")},
            {"id": "pc_hair", "name": "洗发护发", "icon": "bi-scissors",
             "keywords": ("洗发", "护发", "吹风", "理发", "梳")},
            {"id": "pc_body", "name": "身体护理", "icon": "bi-heart-pulse",
             "keywords": ("沐浴", "身体乳", "剃须", "牙膏", "口腔", "卫生")},
            {"id": "pc_clean", "name": "家清用品", "icon": "bi-droplet",
             "keywords": ("洗衣液", "清洁剂", "消毒", "驱蚊", "除螨")},
            {"id": "pc_health", "name": "健康护理", "icon": "bi-bandaid-fill",
             "keywords": ("按摩", "理疗", "颈椎", "护腰", "血压", "体温")},
        ],
    },
    {
        "id": "auto",
        "name": "汽车用品",
        "hint": "家居日用",
        "icon": "bi-car-front",
        "accent": "indigo",
        "children": [
            {"id": "at_acc", "name": "汽车配件", "icon": "bi-gear-wide-connected",
             "keywords": ("汽车", "车载", "车用", "座套", "脚垫", "车衣")},
            {"id": "at_charge", "name": "车载电器", "icon": "bi-battery-charging",
             "keywords": ("车载充电", "行车记录", "导航", "车载支架", "胎压")},
            {"id": "at_care", "name": "汽车美容", "icon": "bi-brush-fill",
             "keywords": ("洗车", "打蜡", "镀晶", "内饰清洁", "玻璃水")},
            {"id": "at_tool", "name": "随车工具", "icon": "bi-cone-striped",
             "keywords": ("千斤顶", "拖车", "警示", "急救包", "充气泵")},
            {"id": "at_daily", "name": "日用杂货", "icon": "bi-basket2",
             "keywords": ("日用", "百货", "一次性", "百货", "挂钩", "雨伞")},
            {"id": "at_pet", "name": "宠物用品", "icon": "bi-heart",
             "keywords": ("宠物", "猫", "狗", "宠物玩具", "猫砂")},
        ],
    },
    {
        "id": "food",
        "name": "食品生鲜",
        "hint": "茶饮冲调",
        "icon": "bi-cup-straw",
        "accent": "green",
        "children": [
            {"id": "fd_snack", "name": "休闲零食", "icon": "bi-basket-fill",
             "keywords": ("零食", "坚果", "糖果", "饼干", "肉干", "果干")},
            {"id": "fd_tea", "name": "茶饮冲调", "icon": "bi-cup",
             "keywords": ("茶", "咖啡", "冲调", "奶粉", "豆粉", "蜂蜜")},
            {"id": "fd_fresh", "name": "生鲜果蔬", "icon": "bi-basket3",
             "keywords": ("生鲜", "水果", "蔬菜", "牛肉", "海鲜", "米", "粮", "油")},
            {"id": "fd_local", "name": "地方特产", "icon": "bi-geo-alt",
             "keywords": ("特产", "礼盒", "干货", "腊味", "酱")},
            {"id": "fd_health", "name": "营养保健", "icon": "bi-capsule",
             "keywords": ("维生素", "钙", "保健品", "益生菌", "蛋白粉")},
            {"id": "fd_wine", "name": "酒水饮料", "icon": "bi-cup-hot-fill",
             "keywords": ("酒", "饮料", "矿泉水", "汽水", "果汁")},
        ],
    },
    {
        "id": "baby",
        "name": "母婴玩具",
        "hint": "童装童鞋",
        "icon": "bi-balloon",
        "accent": "violet",
        "children": [
            {"id": "bb_toy", "name": "玩具", "icon": "bi-puzzle",
             "keywords": ("玩具", "积木", "娃娃", "模型", "遥控", "泡泡")},
            {"id": "bb_care", "name": "母婴用品", "icon": "bi-balloon",
             "keywords": ("母婴", "奶瓶", "尿", "推车", "婴儿", "孕妇", "抱被")},
            {"id": "bb_cloth", "name": "童装童鞋", "icon": "bi-person-arms-up",
             "keywords": ("童装", "儿童", "宝宝", "童鞋", "亲子")},
            {"id": "bb_feed", "name": "喂养洗护", "icon": "bi-cup-straw",
             "keywords": ("辅食", "喂养", "沐浴", "湿巾", "纸尿")},
            {"id": "bb_edu", "name": "学习教育", "icon": "bi-mortarboard",
             "keywords": ("学习", "早教", "点读", "绘本", "文具")},
            {"id": "bb_out", "name": "户外出行", "icon": "bi-scooter",
             "keywords": ("安全座椅", "滑板车", "儿童车", "背带")},
        ],
    },
    {
        "id": "other",
        "name": "其他",
        "hint": "未归类",
        "icon": "bi-three-dots",
        "accent": "slate",
        "children": [
            {"id": "other_sub", "name": "其他", "icon": "bi-three-dots",
             "keywords": ("其他", "杂项", "未分类")},
        ],
    },
]

SOURCING_CATEGORY_FALLBACK = "其他"
SOURCING_CATEGORY_FALLBACK_HINT = "未分类货源"


# =====================================================================
# 货源广场三级类目（1688 类目栏「二级 → 再下级」逐层悬浮展开）
# =====================================================================
# 键 = 二级类目 id；值 = 三级类目名（名字本身即命中标题的关键词）。
# 取名取自真实货源标题词池，保证悬浮面板上的三级类目数字来自真实数据。
SOURCING_SUB_LEAVES: dict[str, tuple[str, ...]] = {
    # —— 女装男装 ——
    "ap_women": ("连衣裙", "针织衫", "打底衫", "羽绒外套", "风衣外套"),
    "ap_men": ("衬衫", "T恤", "卫衣", "夹克", "休闲裤"),
    "ap_homewear": ("珊瑚绒睡衣", "浴袍", "内衣", "袜子", "家居服"),
    "ap_acc": ("围巾", "丝巾", "腰带", "手套", "帽子"),
    "ap_jewelry": ("项链", "手链", "戒指", "发饰", "耳饰", "腕表"),
    "ap_bag": ("双肩背包", "钱包", "行李箱", "卡包", "托特包", "斜挎"),
    "ap_shoes": ("运动鞋", "马丁靴", "拖鞋", "帆布鞋", "网面鞋", "切尔西靴"),
    # —— 办公文化 ——
    "of_stationery": ("中性笔", "活页笔记本", "文具盒", "量角器", "橡皮", "试卷收纳"),
    "of_paper": ("复印纸", "便签纸", "标签纸", "票据纸", "档案袋", "小票纸"),
    "of_device": ("碎纸机", "装订机", "办公椅", "白板", "计算器", "白板笔"),
    "of_book": ("绘本", "畅销书", "手办", "拼图", "桌面游戏", "音像"),
    "of_sport": ("瑜伽垫", "哑铃", "跳绳", "拉力带", "减震垫", "篮球"),
    "of_outdoor": ("折叠桌椅", "帐篷", "露营灯", "登山杖", "骑行头盔", "钓鱼竿"),
    "of_gift": ("礼品套装", "钥匙扣", "摆件", "徽章", "伴手礼", "纪念币"),
    # —— 餐饮饮具 ——
    "tw_cup": ("保温杯", "玻璃杯", "水壶", "茶壶", "随手杯", "吸管"),
    "tw_kitchen": ("刀具套装", "不粘锅", "砂锅煲", "铲勺套装", "合金筷", "烘焙工具"),
    "tw_tableware": ("餐具套装", "便当盒", "饭盒", "餐垫", "骨瓷碗", "盘子"),
    "tw_storage": ("收纳箱", "置物架", "挂架", "储物篮", "收纳袋", "整理盒"),
    "tw_clean": ("平板拖把", "抹布", "洗洁精", "清洁刷", "垃圾袋", "扫把套装"),
    "tw_laundry": ("衣架", "晾晒架", "洗衣袋", "熨衣机", "晒衣夹", "晒被架"),
    "tw_kitchen_small": ("电饭煲", "电煮锅", "破壁料理机", "榨汁机", "空气炸锅", "电热锅"),
    # —— 家用电器 ——
    "dg_pc": ("机械键盘", "无线鼠标", "显示器", "键鼠套装", "移动硬盘", "U盘"),
    "dg_phone_acc": ("手机壳", "钢化膜", "手机支架", "数据线", "充电器", "充电宝"),
    "dg_audio": ("蓝牙耳机", "头戴式耳机", "蓝牙音箱", "音响", "麦克风", "游戏耳麦"),
    "dg_charge": ("无线充电器", "充电头", "快充线", "充电底座", "车载快充", "快充套装"),
    "dg_smart": ("智能摄像头", "路由", "投影仪", "智能手环", "智能手表", "智能门锁"),
    "dg_home_app": ("落地风扇", "加湿器", "取暖器", "吸尘器", "净水器", "空气净化器"),
    "dg_wire": ("高清线", "音频转换头", "拓展坞", "转接线", "排插", "插座面板"),
    # —— 五金工具 ——
    "hw_tools": ("工具箱", "扳手", "螺丝刀套装", "钢丝钳", "羊角锤", "套筒扳手"),
    "hw_electric": ("电线", "墙壁开关", "测电笔", "配电箱", "断路器", "电缆"),
    "hw_machine": ("工业电机", "潜水泵", "电焊机", "切割机", "空压机", "传送设备"),
    "hw_build": ("瓷砖", "防水涂料", "水泥砂浆", "生态板材", "美缝剂", "轻钢龙骨"),
    "hw_lock": ("指纹门锁", "合页", "门把手", "闭门器", "锁芯", "铰链"),
    "hw_measure": ("钢卷尺", "水平仪", "万用表", "游标卡尺", "测温仪", "量具套装"),
    # —— 家纺家饰 ——
    "ht_bed": ("四件套", "乳胶枕", "春秋被", "床单", "记忆棉床垫", "凉席"),
    "ht_decor": ("摆件", "装饰画", "相框", "花瓶", "香薰蜡烛", "家居摆件"),
    "ht_curtain": ("遮光窗帘", "坐垫", "地毯", "地垫", "桌布", "窗帘轨道"),
    "ht_living": ("沙发", "茶几", "电视柜", "餐椅", "餐桌", "衣柜"),
    "ht_bath": ("浴巾", "毛巾", "牙刷", "马桶盖", "浴室挂帘", "花洒"),
    "ht_lighting": ("台灯", "LED灯带", "吸顶灯", "落地灯", "筒灯", "庭院灯"),
    # —— 个护家清 ——
    "pc_skin": ("洗面奶", "面膜", "精华", "面霜", "水乳套装", "防晒霜"),
    "pc_makeup": ("口红", "眼影盘", "粉底液", "香水", "卸妆水", "彩妆套装"),
    "pc_hair": ("洗发水", "护发精油", "吹风机", "理发器", "气囊梳", "护发喷雾"),
    "pc_body": ("沐浴露", "身体乳", "剃须刀", "牙膏", "口腔喷雾", "护理垫"),
    "pc_clean": ("洗衣液", "厨房清洁剂", "消毒液", "驱蚊液", "除螨喷雾", "洗衣机槽清洁剂"),
    "pc_health": ("颈椎按摩仪", "筋膜按摩枪", "理疗灯", "护腰靠垫", "血压计", "体温计"),
    # —— 汽车用品 ——
    "at_acc": ("汽车座套", "车载脚垫", "车用抱枕", "汽车车衣", "汽车挂件", "汽车坐垫"),
    "at_charge": ("行车记录仪", "导航仪", "胎压监测器"),
    "at_care": ("洗车水枪", "打蜡镀膜", "镀晶套装", "内饰清洁剂", "玻璃水", "洗车海绵"),
    "at_tool": ("液压千斤顶", "拖车绳", "警示三角架", "车载急救包", "车载充气泵", "警示牌"),
    "at_daily": ("强力挂钩", "一次性手套", "折叠雨伞", "日用百货", "衣物防尘罩", "免钉胶"),
    "at_pet": ("宠物玩具", "猫砂", "牵引绳", "宠物窝", "逗猫棒", "宠物零食"),
    # —— 食品生鲜 ——
    "fd_snack": ("每日坚果", "零食大礼包", "手工糖果", "曲奇饼干", "手撕肉干", "芒果果干"),
    "fd_tea": ("普洱茶饼", "冷萃咖啡液", "豆浆粉", "全脂奶粉", "蜂蜜", "花果茶"),
    "fd_fresh": ("新鲜水果", "有机蔬菜", "原切牛肉", "冷冻海鲜", "大米", "食用调和油"),
    "fd_local": ("特产礼盒", "腊味", "干货", "香菇酱", "红薯粉条", "鲜花饼"),
    "fd_health": ("维生素C", "液体钙", "益生菌", "乳清蛋白粉", "保健品礼盒", "深海鱼油"),
    "fd_wine": ("精酿啤酒", "白酒", "矿泉水", "碳酸汽水", "NFC果汁", "红酒"),
    # —— 母婴玩具 ——
    "bb_toy": ("益智积木", "遥控玩具车", "毛绒娃娃", "拼装模型", "泡泡机", "解压玩具"),
    "bb_care": ("婴儿奶瓶", "婴儿推车", "孕妇抱枕", "待产包", "婴儿抱被", "隔尿垫"),
    "bb_cloth": ("儿童套装", "宝宝童装", "亲子装", "童鞋", "儿童保暖套装", "爬服"),
    "bb_feed": ("辅食米粉", "辅食机", "纸尿裤", "手口湿巾", "辅食研磨碗", "喂养勺"),
    "bb_edu": ("早教故事机", "点读笔", "精装绘本", "点读挂图", "益智卡片", "启蒙绘本"),
    "bb_out": ("安全座椅", "滑板车", "儿童车", "背带腰凳"),
}


# =====================================================================
# 采购节点（1688 首页「采购节点」运营位）
# =====================================================================
# 节点本身是运营日历（可配置），节点下的商品由 keywords 在货源标题里实时匹配，
# 不做写死的商品绑定。
#   start / end：MM-DD（end < start 视为跨年节点，如年货节）
#   keywords   ：商品标题命中任一关键词即归入该节点（空 = 通吃全品类）
PURCHASE_NODES: list[dict] = [
    {"id": "mid_autumn", "name": "中秋节", "start": "09-19", "end": "09-25",
     "icon": "bi-moon-stars", "keywords": ("礼盒", "礼", "茶", "月饼", "坚果", "特产", "果")},
    {"id": "national_day", "name": "国庆黄金周", "start": "09-28", "end": "10-07",
     "icon": "bi-suitcase-lg", "keywords": ("便携", "户外", "旅行", "帐篷", "背包", "保温杯", "防晒")},
    {"id": "winter_new", "name": "冬装上新", "start": "10-08", "end": "10-23",
     "icon": "bi-snow2", "keywords": ("冬季", "加厚", "保暖", "珊瑚绒", "羽绒", "睡衣", "毛衣", "绒")},
    {"id": "double11", "name": "双11大促", "start": "11-01", "end": "11-11",
     "icon": "bi-lightning-charge-fill", "keywords": ()},
    {"id": "new_year", "name": "年货节", "start": "12-25", "end": "01-20",
     "icon": "bi-gift-fill", "keywords": ("年货", "坚果", "糖果", "礼盒", "对联", "红包")},
]


def purchase_node_windows(today: "date | None" = None) -> list[dict]:
    """计算采购节点的当前状态（进行中 / 即将开始），进行中的排最前、其余按临近排序。

    节点配置由运营在渔枢系统「货源 · 采购节点」页维护（同库的 purchase_nodes 表，带
    60 秒缓存，改完这边最多 60 秒生效）；读不到（独立部署 / 表还没播种）就回落到内置的
    PURCHASE_NODES 常量。跨年节点（end < start，如 12-25 ~ 01-20）自动落到次年。
    """
    from datetime import date as _date

    day = today or _date.today()
    raw: list[dict] = [
        {
            "id": str(node.get("node_key") or ""),
            "name": str(node.get("name") or ""),
            "icon": str(node.get("icon") or "bi-calendar-event"),
            "start": str(node.get("start_md") or ""),
            "end": str(node.get("end_md") or ""),
            "keywords": tuple(node.get("keywords") or ()),
            "sort_order": int(node.get("sort_order") or 0),
        }
        for node in hub_purchase_nodes()
    ]
    if not raw:
        raw = [
            {"id": n["id"], "name": n["name"], "icon": n["icon"],
             "start": n["start"], "end": n["end"], "keywords": tuple(n["keywords"]),
             "sort_order": idx * 10}
            for idx, n in enumerate(PURCHASE_NODES)
        ]

    out: list[dict] = []
    for node in raw:
        try:
            sm, sd = (int(x) for x in str(node["start"]).split("-"))
            em, ed = (int(x) for x in str(node["end"]).split("-"))
        except (TypeError, ValueError):  # 配错日期的节点直接跳过，不拖垮整个运营位
            continue
        start = _date(day.year, sm, sd)
        end = _date(day.year, em, ed)
        if end < start:  # 跨年：窗口落到下一年（如 12-25 ~ 01-20）
            end = _date(day.year + 1, em, ed)
        ongoing = start <= day <= end
        days_to_start = (start - day).days
        out.append({
            "id": node["id"],
            "name": node["name"],
            "icon": node["icon"],
            "keywords": node["keywords"],
            "start": start,
            "end": end,
            "date_text": f"{start.month}.{start.day}-{end.month}.{end.day}",
            "status": "ongoing" if ongoing else ("upcoming" if days_to_start >= 0 else "passed"),
            "days_to_start": days_to_start,
            "days_left": (end - day).days if ongoing else 0,
            # 运营在「货源 · 采购节点」里配的排序：同一状态组内它说了算
            "sort_order": int(node.get("sort_order") or 0),
        })
    # 排序口径（与渔枢系统「采购节点」页的预览完全一致）：
    # 进行中的最前 → 即将开始的居中 → 已结束的沉底；同组内按运营排序号，再按临近程度。
    out.sort(key=lambda n: (
        n["status"] == "passed",
        n["status"] != "ongoing",
        n["sort_order"],
        abs(n["days_to_start"]),
    ))
    return out


# ---------------------------------------------------------------------
# 采购节点配置：渔枢系统「货源 · 采购节点」维护的 purchase_nodes 表
# ---------------------------------------------------------------------
# 与类目同一套读法：同库直读 + 60 秒缓存；读不到就交回上面的内置常量兜底。
_HUB_NODE_SQL = (
    "SELECT node_key, name, icon, start_md, end_md, keywords, sort_order "
    "FROM purchase_nodes WHERE enabled ORDER BY sort_order, id"
)
_HUB_NODE_TTL = 60.0
_hub_node_cache: tuple[float, list[dict]] | None = None


def hub_purchase_nodes(force: bool = False) -> list[dict]:
    """读渔枢系统采购节点配置（带 60 秒缓存）；读不到返回空列表。"""
    global _hub_node_cache
    now = time.time()
    if not force and _hub_node_cache and now - _hub_node_cache[0] < _HUB_NODE_TTL:
        return list(_hub_node_cache[1])

    from sqlalchemy import text  # noqa: PLC0415  延迟导入：模块被 import 时不碰数据库

    rows: list[dict] = []
    scope = _session_scope()
    if scope is not None:
        try:
            with scope() as db:
                for row in db.execute(text(_HUB_NODE_SQL)):
                    data = dict(row._mapping)  # noqa: SLF001  SQLAlchemy Row → dict
                    keywords = [
                        word.strip()
                        for line in str(data.get("keywords") or "").replace("，", "\n").replace(",", "\n").splitlines()
                        for word in [line.strip()]
                        if word.strip()
                    ]
                    rows.append({
                        "node_key": str(data.get("node_key") or "").strip(),
                        "name": str(data.get("name") or "").strip(),
                        "icon": str(data.get("icon") or "bi-calendar-event").strip(),
                        "start_md": str(data.get("start_md") or "").strip(),
                        "end_md": str(data.get("end_md") or "").strip(),
                        "keywords": tuple(keywords),
                        "sort_order": int(data.get("sort_order") or 0),
                    })
        except Exception as exc:  # noqa: BLE001  读不到不该把货源广场打挂
            logger.warning("读取渔枢系统采购节点失败，改用内置节点：%s", exc)
            rows = []
    _hub_node_cache = (now, rows)
    return list(rows)


# ---------------------------------------------------------------------
# 货源展示基数：渔枢系统「货源 · 展示基数」维护的 sourcing_display_config 表
# ---------------------------------------------------------------------
# 2026-10-13 用户拍板：卡片「X 家上架 / 已售 N」显示「基数 + 真实数量」，
# 真实值照旧单独算（排序 / 热销榜 / 详情页在售闲鱼号都用真实值）。读不到就当 0。
_HUB_DISPLAY_SQL = (
    "SELECT listed_shops_base, sold_base FROM sourcing_display_config "
    "ORDER BY id LIMIT 1"
)
_HUB_DISPLAY_TTL = 60.0
_hub_display_cache: tuple[float, dict] | None = None


def hub_display_base(force: bool = False) -> dict:
    """读渔枢系统配的展示基数（带 60 秒缓存）；读不到返回全 0。"""
    global _hub_display_cache
    now = time.time()
    if not force and _hub_display_cache and now - _hub_display_cache[0] < _HUB_DISPLAY_TTL:
        return dict(_hub_display_cache[1])

    from sqlalchemy import text  # noqa: PLC0415  延迟导入：模块被 import 时不碰数据库

    out = {"listed_shops_base": 0, "sold_base": 0}
    scope = _session_scope()
    if scope is not None:
        try:
            with scope() as db:
                row = db.execute(text(_HUB_DISPLAY_SQL)).first()
                if row is not None:
                    data = dict(row._mapping)  # noqa: SLF001
                    out = {
                        "listed_shops_base": int(data.get("listed_shops_base") or 0),
                        "sold_base": int(data.get("sold_base") or 0),
                    }
        except Exception as exc:  # noqa: BLE001  表还没建 / 分端部署读不到，都当 0
            logger.warning("读取渔枢系统展示基数失败，按 0 处理：%s", exc)
    _hub_display_cache = (now, out)
    return dict(out)


# 节点下**手动配置**的商品（2026-10-11）：运营在渔枢系统「采购节点」页指定
# 「这个活动就推这几款」，渔货栈把它们排在这个活动的最前面；
# 关键词匹配那套系统规则一点不变（两边合起来去重后就是这个活动的货）。
_HUB_NODE_ITEM_SQL = (
    "SELECT n.node_key AS node_key, i.item_id AS item_id "
    "FROM purchase_node_items i JOIN purchase_nodes n ON n.id = i.node_id "
    "WHERE i.enabled AND n.enabled "
    "ORDER BY n.node_key, i.sort_order, i.id"
)
_HUB_NODE_ITEM_TTL = 60.0
_hub_node_item_cache: tuple[float, dict[str, list[str]]] | None = None


def hub_node_items(force: bool = False) -> dict[str, list[str]]:
    """手动配置的商品：{节点标识: [货源编号, ...]}（按运营排好的顺序，带 60 秒缓存）。"""
    global _hub_node_item_cache
    now = time.time()
    if (
        not force
        and _hub_node_item_cache
        and now - _hub_node_item_cache[0] < _HUB_NODE_ITEM_TTL
    ):
        return {k: list(v) for k, v in _hub_node_item_cache[1].items()}

    from sqlalchemy import text  # noqa: PLC0415  延迟导入：模块被 import 时不碰数据库

    out: dict[str, list[str]] = {}
    scope = _session_scope()
    if scope is not None:
        try:
            with scope() as db:
                for row in db.execute(text(_HUB_NODE_ITEM_SQL)):
                    data = dict(row._mapping)  # noqa: SLF001
                    key = str(data.get("node_key") or "").strip()
                    item_id = str(data.get("item_id") or "").strip()
                    if key and item_id:
                        out.setdefault(key, []).append(item_id)
        except Exception as exc:  # noqa: BLE001  表还没建 / 读不到都不影响广场
            logger.warning("读取采购节点手动配置的商品失败：%s", exc)
            out = {}
    _hub_node_item_cache = (now, out)
    return {k: list(v) for k, v in out.items()}


# =====================================================================
# 类目唯一数据源：渔枢系统「货源 · 类目管理」的那张 product_categories 表
# =====================================================================
# 口径（2026-09-28 起）：
#   · 类目只在渔枢系统维护一份（见 app/services/category.py 与「类目管理」页）；
#   · 渔货栈这里直接读同一张表（同进程时连的就是渔枢系统的库），改名 / 新增 / 停用立刻生效；
#   · 读不到（独立部署没接渔枢系统、表还没播种）就回落到上面那份内置常量，页面照旧能开。
#
# 注意：这些函数只在被调用时才碰数据库 —— app/services/category.py 播种时要 import
# 本模块的常量，如果这里在 import 期就读表会绕成死循环。
_HUB_CATEGORY_TTL = 60.0
_hub_category_cache: tuple[float, list[dict]] | None = None

_HUB_CATEGORY_SQL = (
    "SELECT id, parent_id, level, name, sort_order, enabled, source_id "
    "FROM product_categories ORDER BY sort_order, id"
)

# 内置常量里一级 / 二级类目的「素材」：图标、色板、关键词（名称不变时继续沿用）
_ACCENT_POOL: tuple[str, ...] = ("rose", "slate", "amber", "teal", "violet", "sky", "lime", "coral")


def _top_meta() -> dict[str, dict]:
    return {
        node["name"]: {
            "id": node["id"], "hint": node.get("hint", ""),
            "icon": node.get("icon", "bi-tag"), "accent": node.get("accent", ""),
        }
        for node in SOURCING_CATEGORY_TREE
    }


def _sub_meta() -> dict[tuple[str, str], dict]:
    return {
        (node["name"], child["name"]): {
            "id": child["id"],
            "icon": child.get("icon", "bi-dot"),
            "keywords": child.get("keywords") or (child["name"],),
        }
        for node in SOURCING_CATEGORY_TREE
        for child in node["children"]
    }


# ---------------------------------------------------------------------
# 类目图标：按类目语义生成
# ---------------------------------------------------------------------
# 类目表换成 1688 那套之后是 52 个一级 / 上千个二级，逐个手写图标不现实。
# 生成顺序：内置素材（名字对得上就用）→ 一级类目名精确表 → 二级按关键词规则
# （长词优先，免得「面包」被「包」抢走）→ 继承一级类目的图标 → bi-tag。
_ICON_BY_TOP: dict[str, str] = {
    "农业": "bi-tree",
    "食品酒水": "bi-basket",
    "纺织、皮革": "bi-rulers",
    "电工电气": "bi-lightning-charge",
    "家用电器": "bi-tv",
    "数码、电脑": "bi-laptop",
    "化工": "bi-eyedropper",
    "冶金矿产": "bi-gem",
    "能源": "bi-battery-full",
    "交通运输": "bi-truck",
    "家装建材": "bi-bricks",
    "居家日用品": "bi-basket3",
    "运动户外": "bi-bicycle",
    "传媒、广电": "bi-broadcast",
    "服饰配件、饰品": "bi-gem",
    "橡塑": "bi-hexagon",
    "电子元器件": "bi-cpu",
    "灯饰照明": "bi-lightbulb",
    "五金、工具": "bi-tools",
    "环保": "bi-recycle",
    "机械及行业设备": "bi-gear",
    "医药、保养": "bi-heart-pulse",
    "办公、文化": "bi-pencil-square",
    "包装": "bi-box-seam",
    "商务服务": "bi-briefcase",
    "安全、防护": "bi-shield-check",
    "汽摩及配件": "bi-speedometer2",
    "印刷": "bi-printer",
    "家纺家饰": "bi-house-heart",
    "美容护肤/彩妆": "bi-brush",
    "童装": "bi-emoji-smile",
    "内衣": "bi-heart",
    "通信产品": "bi-telephone",
    "机床": "bi-gear-fill",
    "玩具": "bi-controller",
    "加工": "bi-hammer",
    "二手设备转让": "bi-arrow-repeat",
    "男装": "bi-person-badge",
    "女装": "bi-person-standing-dress",
    "鞋": "bi-bag",
    "箱包皮具": "bi-handbag",
    "宠物及园艺": "bi-flower1",
    "汽车用品": "bi-car-front",
    "钢铁": "bi-layers",
    "仪器仪表": "bi-speedometer",
    "餐饮生鲜": "bi-egg-fried",
    "个护/家清": "bi-droplet",
    "成人用品": "bi-bag-heart",
    "建材": "bi-house-door",
    "日用餐厨饮具": "bi-cup-hot",
    "收纳清洁用具": "bi-archive",
    "新能源": "bi-battery-charging",
    "其他": "bi-three-dots",
    # 老的那套一级（类目表换成 1688 之前渔货栈内置的 10 个）：也写死，
    # 免得靠关键词命中（「母婴玩具」会命中「玩具」给手柄图标之类）
    "女装男装": "bi-bag-heart",
    "办公文化": "bi-pencil-square",
    "餐饮饮具": "bi-cup-hot",
    "五金工具": "bi-tools",
    "家纺家饰": "bi-house-heart",
    "个护家清": "bi-droplet",
    "汽车用品": "bi-car-front",
    "食品生鲜": "bi-basket2",
    "母婴玩具": "bi-balloon",
}

# （关键词 → 图标）：命中名字里最长的那个词
_ICON_RULES: tuple[tuple[str, str], ...] = (
    ("连衣裙", "bi-person-standing-dress"), ("女装", "bi-person-standing-dress"),
    ("女式", "bi-person-standing-dress"),
    ("男装", "bi-person-badge"), ("男式", "bi-person-badge"),
    ("童装", "bi-emoji-smile"), ("婴儿", "bi-emoji-smile"), ("孕产", "bi-emoji-smile"),
    ("内衣", "bi-heart"), ("文胸", "bi-heart"), ("睡衣", "bi-moon-stars"),
    ("鞋", "bi-bag"), ("靴", "bi-bag"),
    ("箱包", "bi-handbag"), ("钱包", "bi-wallet2"), ("背包", "bi-backpack"),
    ("手机", "bi-phone"), ("对讲", "bi-phone"), ("通讯", "bi-telephone"),
    ("笔记本", "bi-laptop"), ("电脑", "bi-laptop"), ("平板", "bi-tablet"), ("台式", "bi-pc-display"),
    ("耳机", "bi-headphones"), ("音响", "bi-speaker"), ("音箱", "bi-speaker"), ("影音", "bi-speaker"),
    ("相机", "bi-camera"), ("摄影", "bi-camera"), ("镜头", "bi-camera"),
    ("电视", "bi-tv"), ("显示", "bi-display"), ("投影", "bi-projector"),
    ("冰箱", "bi-snow"), ("空调", "bi-snow"), ("洗衣机", "bi-thermometer-half"),
    ("厨房", "bi-cup-hot"), ("厨具", "bi-cup-hot"), ("餐具", "bi-cup-hot"), ("饮具", "bi-cup-straw"),
    ("水杯", "bi-cup-straw"), ("保温", "bi-thermometer"),
    ("零食", "bi-cookie"), ("酒", "bi-cup-straw"), ("饮料", "bi-cup-straw"),
    ("生鲜", "bi-egg-fried"), ("食材", "bi-basket"), ("食品", "bi-basket"), ("茶叶", "bi-flower3"),
    ("美妆", "bi-brush"), ("护肤", "bi-droplet-half"), ("彩妆", "bi-brush"), ("化妆", "bi-brush"),
    ("香水", "bi-droplet"), ("美容", "bi-magic"),
    ("个护", "bi-droplet"), ("清洁", "bi-eraser"), ("洗护", "bi-droplet"), ("家清", "bi-eraser"),
    ("家纺", "bi-house-heart"), ("床上", "bi-house-heart"), ("被", "bi-house-heart"),
    ("枕", "bi-house-heart"), ("窗帘", "bi-house-heart"), ("布艺", "bi-palette"),
    ("灯", "bi-lightbulb"), ("照明", "bi-lightbulb"),
    ("家具", "bi-lamp"), ("沙发", "bi-lamp"), ("收纳", "bi-archive"),
    ("五金", "bi-tools"), ("工具", "bi-tools"), ("螺丝", "bi-tools"), ("刀", "bi-scissors"),
    ("汽车", "bi-car-front"), ("车载", "bi-car-front"), ("轮胎", "bi-circle"),
    ("摩托", "bi-bicycle"), ("自行车", "bi-bicycle"), ("骑行", "bi-bicycle"),
    ("运动", "bi-activity"), ("健身", "bi-activity"), ("户外", "bi-tree"), ("露营", "bi-tree"),
    ("玩具", "bi-controller"), ("益智", "bi-puzzle"), ("模型", "bi-joystick"),
    ("宠物", "bi-flower1"), ("猫", "bi-flower1"), ("狗", "bi-flower1"), ("园艺", "bi-flower1"),
    ("文具", "bi-pencil"), ("办公", "bi-printer"), ("笔", "bi-pen"), ("纸", "bi-file-earmark-text"),
    ("图书", "bi-book"), ("教育", "bi-mortarboard"), ("乐器", "bi-music-note"),
    ("包装", "bi-box-seam"), ("印刷", "bi-printer"),
    ("医疗", "bi-heart-pulse"), ("保健", "bi-heart-pulse"), ("药", "bi-capsule"),
    ("机械", "bi-gear"), ("设备", "bi-gear"), ("机床", "bi-gear-fill"), ("模具", "bi-gear"),
    ("电子", "bi-cpu"), ("元器件", "bi-cpu"), ("芯片", "bi-cpu"), ("电路", "bi-motherboard"),
    ("化工", "bi-eyedropper"), ("橡塑", "bi-hexagon"), ("塑料", "bi-hexagon"), ("胶", "bi-droplet-half"),
    ("钢铁", "bi-layers"), ("金属", "bi-layers"), ("冶金", "bi-gem"), ("矿产", "bi-gem"),
    ("能源", "bi-battery-full"), ("电池", "bi-battery-charging"), ("太阳能", "bi-sun"),
    ("配电", "bi-plugin"), ("开关", "bi-toggle-on"), ("电线", "bi-bezier"),
    ("服饰", "bi-bag-heart"), ("配件", "bi-bandaid"), ("饰品", "bi-gem"),
    ("首饰", "bi-gem"), ("珠宝", "bi-gem"), ("手表", "bi-watch"), ("眼镜", "bi-eyeglasses"),
    ("家装", "bi-bricks"), ("建材", "bi-house-door"), ("卫浴", "bi-droplet"), ("瓷砖", "bi-bricks"),
    ("劳保", "bi-shield-check"), ("安防", "bi-shield-lock"), ("消防", "bi-fire"),
    ("农业", "bi-tree"), ("养殖", "bi-tree"), ("农资", "bi-flower1"),
    ("二手", "bi-arrow-repeat"), ("加工", "bi-hammer"), ("定制", "bi-palette"),
    ("商务", "bi-briefcase"), ("服务", "bi-person-badge"), ("物流", "bi-truck"),
    ("广告", "bi-megaphone"), ("传媒", "bi-broadcast"), ("礼品", "bi-gift"),
    ("成人", "bi-bag-heart"), ("环保", "bi-recycle"), ("净化", "bi-recycle"),
)

_ICON_FALLBACK = "bi-tag"


def category_icon(name: str, parent_icon: str = "") -> str:
    """按类目名生成 Bootstrap 图标名（内置素材 → 一级表 → 关键词 → 继承父级 → bi-tag）。"""
    text_name = str(name or "").strip()
    if not text_name:
        return parent_icon or _ICON_FALLBACK
    icon = _ICON_BY_TOP.get(text_name)
    if icon:
        return icon
    hit_key = ""
    for key, icon_name in _ICON_RULES:
        if key in text_name and len(key) > len(hit_key):
            hit_key, icon = key, icon_name
    if icon:
        return icon
    return parent_icon or _ICON_FALLBACK


def _session_scope():
    """拿一个能连到渔枢系统库的会话上下文：优先渔枢系统自己的连接，其次渔货栈自己的。"""
    import importlib

    for module_name in ("app.db", "src.deps"):
        try:
            return importlib.import_module(module_name).session_scope
        except Exception:  # noqa: BLE001  独立部署或还没初始化时换下一个
            continue
    return None


def hub_category_rows(force: bool = False) -> list[dict] | None:
    """读渔枢系统类目表的全部节点（带 60 秒缓存）；读不到返回 None（交给内置常量兜底）。"""
    global _hub_category_cache
    now = time.time()
    if not force and _hub_category_cache and now - _hub_category_cache[0] < _HUB_CATEGORY_TTL:
        return _hub_category_cache[1] or None

    from sqlalchemy import text  # noqa: PLC0415  延迟导入：模块被 import 时不碰数据库

    rows: list[dict] = []
    scope = _session_scope()
    if scope is not None:
        try:
            with scope() as db:
                for row in db.execute(text(_HUB_CATEGORY_SQL)):
                    data = dict(row._mapping)  # noqa: SLF001  SQLAlchemy Row → dict
                    rows.append({
                        "id": int(data.get("id") or 0),
                        "parent_id": int(data["parent_id"]) if data.get("parent_id") is not None else None,
                        "level": int(data.get("level") or 1),
                        "name": str(data.get("name") or "").strip(),
                        "sort_order": int(data.get("sort_order") or 0),
                        "enabled": bool(data.get("enabled", True)),
                        "source_id": str(data.get("source_id") or ""),
                    })
        except Exception as exc:  # noqa: BLE001  读不到不该把页面打挂
            logger.warning("读取渔枢系统类目表失败，改用内置类目：%s", exc)
            rows = []
    _hub_category_cache = (now, rows)
    return rows or None


# =====================================================================
# 商品标签：字典在渔枢系统「货源 · 商品标签」维护（product_tags 表）
# =====================================================================
# 渔货栈要按标签配色（brand 金 / success 绿 / info 蓝 / danger 红），所以把
# 「标签名 → 配色」读一份过来；读不到（独立部署 / 表还没播种）回落内置映射，
# 认不出来的标签一律 info —— 页面不会因为读不到字典就打不开。
_HUB_TAG_SQL = "SELECT name, kind FROM product_tags"
_HUB_TAG_TTL = 60.0
_hub_tag_cache: tuple[float, dict[str, str]] | None = None

# 内置兜底配色（渔枢系统还没播种 / 连不上库时用）
_TAG_KIND_BUILTIN: dict[str, str] = {
    "源头工厂": "brand", "工厂直供": "brand", "工厂直销": "brand",
    "出口品质": "brand", "大牌代工": "brand", "厂家直发": "brand",
    "现货速发": "success", "现货秒发": "success", "包邮": "success",
    "七天无理由": "success", "支持定制": "success",
}


def _normalize_tag_kind(value) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in ("brand", "success", "info", "warn", "danger") else "info"


def hub_tag_kinds(force: bool = False) -> dict[str, str]:
    """读渔枢系统标签字典的「标签名 → 配色」（带 60 秒缓存）；读不到返回空。"""
    global _hub_tag_cache
    now = time.time()
    if not force and _hub_tag_cache and now - _hub_tag_cache[0] < _HUB_TAG_TTL:
        return dict(_hub_tag_cache[1])

    from sqlalchemy import text  # noqa: PLC0415  延迟导入：模块被 import 时不碰数据库

    out: dict[str, str] = {}
    scope = _session_scope()
    if scope is not None:
        try:
            with scope() as db:
                for row in db.execute(text(_HUB_TAG_SQL)):
                    data = dict(row._mapping)  # noqa: SLF001  SQLAlchemy Row → dict
                    name = str(data.get("name") or "").strip()
                    if name:
                        out[name] = _normalize_tag_kind(data.get("kind"))
        except Exception as exc:  # noqa: BLE001  读不到不该把页面打挂
            logger.warning("读取渔枢系统商品标签字典失败，用内置配色兜底：%s", exc)
            out = {}
    _hub_tag_cache = (now, out)
    return dict(out)


def tag_kind_of(name: str) -> str:
    """单个标签的配色：渔枢字典 → 内置映射 → info。"""
    text_name = str(name or "").strip()
    if not text_name:
        return "info"
    return hub_tag_kinds().get(text_name) or _TAG_KIND_BUILTIN.get(text_name, "info")


def split_item_tags(value) -> list[str]:
    """商品上的标签文本（一行一个，兼容逗号 / 顿号）→ 去重保序列表。"""
    raw = str(value or "").replace("，", "\n").replace(",", "\n").replace("、", "\n")
    out: list[str] = []
    for line in raw.splitlines():
        text_name = line.strip()
        if text_name and text_name not in out:
            out.append(text_name)
    return out


def reset_hub_caches() -> None:
    """清掉从渔枢系统读过来那几个缓存（类目 / 商品标签 / 采购节点 / 类目树 / 展示基数）。

    测试里改完表要立刻看到效果，运维排查“改了没生效”时也可以用同一把钥匙。
    """
    global _hub_category_cache, _hub_tag_cache, _hub_node_cache, _tree_cache
    global _hub_node_item_cache, _hub_display_cache
    _hub_category_cache = None
    _hub_tag_cache = None
    _hub_node_cache = None
    _hub_node_item_cache = None  # 节点下手动配置的商品（改完立刻能看到）
    _hub_display_cache = None  # 展示基数（改完立刻能看到）
    _tree_cache = None
    _match_cached.cache_clear()  # 标题归类结果（类目变了要重算）


def hub_category_source() -> str:
    """当前类目数据来自哪里（页面提示用）。"""
    return "渔枢系统 · 类目管理" if hub_category_rows() else "内置类目（未读到渔枢系统类目表）"


def _tree_from_rows(rows: list[dict]) -> list[dict]:
    """扁平节点 → 三级树（只留启用的；父级停用则整棵子树不出现）。"""
    children: dict[int | None, list[dict]] = {}
    for row in rows:
        if row["enabled"] and row["name"]:
            children.setdefault(row["parent_id"], []).append(row)

    def build(parent_id: int | None, depth: int = 1) -> list[dict]:
        if depth > 3:
            return []
        return [
            {
                "name": row["name"],
                "source_id": row["source_id"],
                "children": build(row["id"], depth + 1),
            }
            for row in children.get(parent_id, [])
        ]

    return build(None)


# 类目树有一万多个节点（1688 那套三级），一屏页面里会被调好几次
# （类目栏 / 三级面板 / 归类关键词各调一次），每次重建都有几十毫秒，
# 所以这里缓存 60 秒（与类目表缓存同寿命），改完类目最多等一分钟生效。
_tree_cache: tuple[float, list[dict]] | None = None
_TREE_TTL = 60.0


def sourcing_tree() -> list[dict]:
    """货源广场三级类目：以渔枢系统「类目管理」为准；读不到时回落内置常量。

    返回结构与 `SOURCING_CATEGORY_TREE` 一致（含 icon / accent / keywords 素材），
    只是「有哪些类目、叫什么名字」来自渔枢系统那张表。返回值只读，别改。
    """
    global _tree_cache
    now = time.time()
    if _tree_cache and now - _tree_cache[0] < _TREE_TTL:
        return _tree_cache[1]

    rows = hub_category_rows()
    if not rows:
        return SOURCING_CATEGORY_TREE

    tops = _top_meta()
    subs = _sub_meta()
    out: list[dict] = []
    for index, top in enumerate(_tree_from_rows(rows)):
        meta = tops.get(top["name"], {})
        node = {
            "id": meta.get("id") or top["source_id"] or f"hub_top_{index}",
            "name": top["name"],
            "hint": meta.get("hint", ""),
            # 类目表换成 1688 那套之后有一级 50+ / 二级上千，内置素材只覆盖了原来
            # 那 10 个一级，其余全靠 category_icon 按类目语义生成（不统一成一个 bi-tag）
            "icon": meta.get("icon") or category_icon(top["name"]),
            "accent": meta.get("accent") or _ACCENT_POOL[index % len(_ACCENT_POOL)],
            "children": [],
        }
        for order, sub in enumerate(top["children"], start=1):
            sub_meta = subs.get((top["name"], sub["name"]), {})
            sub_id = sub_meta.get("id") or sub["source_id"] or f"{node['id']}_{order}"
            leaves = sub["children"]
            node["children"].append({
                "id": sub_id,
                "name": sub["name"],
                # 认不出二级名就跟着一级的图标走（同类目一个视觉符号）
                "icon": sub_meta.get("icon") or category_icon(sub["name"], node["icon"]),
                # 归类关键词：内置素材有就用，没有就把三级名也当关键词（1688 那套
                # 二级没有写 keywords，只认自己名字会把大部分货源漏掉）
                "keywords": sub_meta.get("keywords")
                or (sub["name"],) + tuple(leaf["name"] for leaf in leaves[:6]),
                "children": [
                    {"id": f"{sub_id}_l3_{i}", "name": leaf["name"], "keywords": (leaf["name"],)}
                    for i, leaf in enumerate(leaves, start=1)
                ],
            })
        out.append(node)
    _tree_cache = (now, out)
    return out


def sourcing_leaves() -> dict[str, tuple[str, ...]]:
    """二级类目 id → 三级类目名（渔枢系统表优先，读不到用内置常量）。"""
    rows = hub_category_rows()
    if not rows:
        return SOURCING_SUB_LEAVES
    return {
        sub["id"]: tuple(leaf["name"] for leaf in sub.get("children") or ())
        for top in sourcing_tree()
        for sub in top["children"]
    }


def category_tree() -> list[dict]:
    """发布页的两级类目（一级 → 二级）：与渔枢系统类目表同一份数据。"""
    rows = hub_category_rows()
    if not rows:
        return CATEGORY_TREE
    return [
        {
            "id": top["id"],
            "name": top["name"],
            "children": [{"id": sub["id"], "name": sub["name"]} for sub in top["children"]],
        }
        for top in sourcing_tree()
    ]


def category_path() -> dict[str, str]:
    """二级类目 id → 「一级 / 二级」（老 id 一并保留，历史商品照旧显示得出名字）。"""
    paths = dict(CATEGORY_PATH)
    for top in category_tree():
        for sub in top["children"]:
            paths[sub["id"]] = f"{top['name']} / {sub['name']}"
    return paths


def sourcing_leaf_index() -> list[dict]:
    """展开成二级类目索引（含所属一级类目信息），供归类 / 磁贴渲染使用。"""
    leaves: list[dict] = []
    for parent in sourcing_tree():
        for child in parent["children"]:
            leaves.append({
                "id": child["id"],
                "name": child["name"],
                "icon": child["icon"],
                "keywords": child.get("keywords", ()),
                "parent_id": parent["id"],
                "parent_name": parent["name"],
                "parent_hint": parent["hint"],
                "parent_icon": parent["icon"],
                "accent": parent["accent"],
            })
    return leaves


def sourcing_sub_leaves(sub_id: str | None) -> list[dict]:
    """二级类目 id → 三级类目列表（名称本身即命中标题的关键词）。"""
    if not sub_id:
        return []
    return [
        {"id": f"{sub_id}_l3_{i}", "name": name, "keywords": (name,)}
        for i, name in enumerate(sourcing_leaves().get(sub_id, ()), start=1)
    ]


def sourcing_leaf_names_by_sub() -> dict[str, tuple[str, ...]]:
    """二级类目名 → 三级类目名（标题命中统计 / 悬浮面板逐层展开用）。"""
    sub_names = {leaf["id"]: leaf["name"] for leaf in sourcing_leaf_index()}
    return {
        sub_names[sub_id]: names
        for sub_id, names in sourcing_leaves().items()
        if sub_id in sub_names
    }


def sourcing_l3_owner(leaf_name: str) -> tuple[str, str] | None:
    """三级类目名 → (一级类目名, 二级类目名)；未收录返回 None。"""
    leaves = sourcing_leaves()
    for leaf in sourcing_leaf_index():
        if leaf_name in leaves.get(leaf["id"], ()):
            return leaf["parent_name"], leaf["name"]
    return None


# ---- 供货商 id → 中文店铺名 ----
# demo 货源没有店铺名字段，按 supplier_id 稳定派生（同一 id 每次渲染同名）：
#   城市 + 字号 + 经营类目 + 组织形式
_SUPPLIER_CITIES: tuple[str, ...] = (
    "深圳市", "义乌市", "广州市", "东莞市", "杭州市",
    "佛山市", "泉州市", "汕头市", "温州市", "苏州市",
)
_SUPPLIER_BRANDS: tuple[str, ...] = (
    "宏达", "鑫盛", "优选", "恒泰", "佳诚", "汇通", "瑞丰", "天诚", "顺发", "百川",
)
_SUPPLIER_KINDS: tuple[str, ...] = (
    "贸易有限公司", "实业有限公司", "严选商行", "电子商务有限公司", "供应链有限公司", "商行",
)


def supplier_display_name(supplier_id: str | None, scope: str | None = None) -> str:
    """供货商 id → 中文店铺名。

    scope 传二级类目名（如「耳机音响」），作为店铺经营类目；同一个二级类目下的
    不同供货商会因为 id 尾号不同而落到不同城市 / 字号，避免重名。
    """
    if not supplier_id:
        return "源头供货商"

    seed = 0
    for ch in supplier_id:
        seed = (seed * 131 + ord(ch)) % 1_000_003
    tail = supplier_id.rsplit("_", 1)[-1]
    shift = int(tail) if tail.isdigit() else 0

    scene = (scope or "").split("/")[0].strip()
    if not scene or scene == SOURCING_CATEGORY_FALLBACK:
        scene = "日用百货"

    city = _SUPPLIER_CITIES[(seed + shift) % len(_SUPPLIER_CITIES)]
    brand = _SUPPLIER_BRANDS[(seed // 7 + shift) % len(_SUPPLIER_BRANDS)]
    kind = _SUPPLIER_KINDS[(seed // 13 + shift * 3) % len(_SUPPLIER_KINDS)]
    return f"{city}{brand}{scene}{kind}"


@lru_cache(maxsize=40000)
def _match_cached(title: str) -> tuple[str, str]:
    text = title or ""
    best: tuple[int, dict] | None = None
    for leaf in sourcing_leaf_index():
        score = 0
        for kw in leaf["keywords"]:
            if kw and kw in text:
                # 长关键词更具体，权重更高
                score += len(kw)
        if score and (best is None or score > best[0]):
            best = (score, leaf)
    if best is None:
        return SOURCING_CATEGORY_FALLBACK, SOURCING_CATEGORY_FALLBACK_HINT
    return best[1]["parent_name"], best[1]["name"]


def sourcing_match(title: str) -> tuple[str, str]:
    """按标题关键词把货源归入 (一级类目名, 二级类目名)；未命中回落「其他」。

    类目换成 1688 那套之后有 1495 个二级、上万个关键词，逐款货源扫一遍是几秒级
    （2546 款 × 1 万个关键词）。同一批货源每次请求都要归类一遍，所以按标题缓存
    结果（改完类目调 reset_hub_caches() 清一次）。
    """
    return _match_cached(title or "")


def sourcing_leaf_of(title: str) -> str:
    """只取二级类目名（精选货源 Tab / 磁贴筛选用）。"""
    return sourcing_match(title)[1]


# ---- 规格模板（按类目给默认规格项，前端一键套用）----
SPEC_TEMPLATES: dict[str, list[dict]] = {
    "digital_pc": [
        {"name": "颜色", "values": ["黑色", "白色"]},
        {"name": "版本", "values": ["标准版", "旗舰版"]},
    ],
    "digital_audio": [
        {"name": "颜色", "values": ["曜石黑", "珍珠白", "星空蓝"]},
        {"name": "套餐", "values": ["官方标配", "套餐一", "套餐二"]},
    ],
    "digital_phone": [
        {"name": "颜色", "values": ["黑色", "白色", "蓝色"]},
        {"name": "适配机型", "values": ["通用"]},
    ],
    "digital_smart": [
        {"name": "颜色", "values": ["黑色", "白色"]},
    ],
    "digital_home": [
        {"name": "颜色", "values": ["白色", "灰色"]},
        {"name": "容量", "values": ["标准", "加大"]},
    ],
    "home_bed": [
        {"name": "尺寸", "values": ["1.5m", "1.8m", "2.0m"]},
        {"name": "颜色", "values": ["浅灰", "米白", "雾蓝"]},
    ],
    "home_kitchen": [
        {"name": "容量", "values": ["350ml", "500ml", "750ml"]},
        {"name": "颜色", "values": ["原色", "黑色"]},
    ],
    "home_daily": [
        {"name": "规格", "values": ["单件装", "两件装", "家庭装"]},
    ],
    "home_storage": [
        {"name": "规格", "values": ["小号", "中号", "大号"]},
    ],
    "wear_men": [
        {"name": "颜色", "values": ["黑色", "藏青", "灰色"]},
        {"name": "尺码", "values": ["M", "L", "XL", "2XL"]},
    ],
    "wear_women": [
        {"name": "颜色", "values": ["米白", "杏色", "黑色"]},
        {"name": "尺码", "values": ["S", "M", "L", "XL"]},
    ],
    "wear_shoes": [
        {"name": "颜色", "values": ["黑色", "白色"]},
        {"name": "尺码", "values": ["38", "39", "40", "41", "42"]},
    ],
    "wear_bag": [
        {"name": "颜色", "values": ["黑色", "棕色"]},
    ],
    "beauty_skin": [
        {"name": "规格", "values": ["100ml", "200ml"]},
    ],
    "beauty_makeup": [
        {"name": "色号", "values": ["01 自然色", "02 象牙白", "03 蜜桃色"]},
    ],
    "beauty_hair": [
        {"name": "规格", "values": ["400ml", "750ml"]},
    ],
    "food_snack": [
        {"name": "口味", "values": ["原味", "香辣", "五香"]},
        {"name": "规格", "values": ["500g", "1kg"]},
    ],
    "food_tea": [
        {"name": "规格", "values": ["100g", "250g", "357g 茶饼"]},
    ],
    "food_fresh": [
        {"name": "规格", "values": ["2 斤装", "5 斤装"]},
    ],
    "sport_fitness": [
        {"name": "颜色", "values": ["紫色", "蓝色", "灰色"]},
        {"name": "厚度", "values": ["8mm", "10mm", "15mm"]},
    ],
    "sport_outdoor": [
        {"name": "颜色", "values": ["军绿", "黑色"]},
    ],
    "baby_toy": [
        {"name": "规格", "values": ["基础套装", "豪华套装"]},
    ],
    "baby_care": [
        {"name": "规格", "values": ["单件装", "组合装"]},
    ],
    "tool_hardware": [
        {"name": "规格", "values": ["46 件套", "108 件套"]},
    ],
    "tool_device": [
        {"name": "规格", "values": ["标准款", "工业款"]},
    ],
}

# 无类目时的兜底规格
DEFAULT_SPEC_TEMPLATE: list[dict] = [{"name": "规格", "values": ["标准款"]}]

# 型号 / 成色 / 重量不是商品的固定字段，而是「不同货卖法不同」的**规格维度**：
# 数码看型号与成色，服饰看成色，重货（家居 / 食品 / 运动）看重量。
# 与渔枢系统（app/services/product.py 的 SPEC_TEMPLATES）同一口径：按一级类目追加，
# 用户自己挑、自己填候选值。
CONDITION_SPEC_VALUES: list[str] = ["全新", "几乎全新", "轻微使用痕迹", "明显使用痕迹"]
# key 用二级类目 id 的前缀（dg=家用电器 / ap=女装男装 / ht=家纺家饰 …，见 SOURCING_CATEGORY_TREE）
EXTRA_SPEC_DIMS: dict[str, list[dict]] = {
    # 数码 / 小家电：看型号与成色
    "dg": [{"name": "型号", "values": []}, {"name": "成色", "values": CONDITION_SPEC_VALUES}],
    # 服饰：看成色（二手服饰）
    "ap": [{"name": "成色", "values": CONDITION_SPEC_VALUES}],
    # 重货：家纺 / 餐饮饮具 / 食品 / 五金 —— 看重量
    "ht": [{"name": "重量", "values": []}],
    "tw": [{"name": "重量", "values": []}],
    "fd": [{"name": "重量", "values": []}],
    "hw": [{"name": "重量", "values": []}],
}


# 类目 id 别名：老模板按 digital_pc / wear_women 这套写，实际类目树用的是 dg_pc / ap_women，
# 在这里对上，不然规格模板永远落兜底。
_SPEC_TEMPLATE_ALIAS: dict[str, str] = {
    "dg_pc": "digital_pc", "dg_phone_acc": "digital_phone", "dg_audio": "digital_audio",
    "dg_smart": "digital_smart", "dg_home_app": "digital_home", "dg_wire": "digital_phone",
    "ap_women": "wear_women", "ap_men": "wear_men", "ap_shoes": "wear_shoes", "ap_bag": "wear_bag",
    "ht_bed": "home_bed", "ht_kitchen": "home_kitchen", "ht_daily": "home_daily",
    "ht_storage": "home_storage", "ht_decor": "home_daily", "ht_curtain": "home_bed",
    "tw_cup": "home_kitchen", "tw_kitchen": "home_kitchen", "tw_tableware": "home_kitchen",
    "fd_snack": "food_snack", "fd_tea": "food_tea", "fd_fresh": "food_fresh",
    "pc_skin": "beauty_skin", "pc_makeup": "beauty_makeup", "pc_hair": "beauty_hair",
    "bb_toy": "baby_toy", "bb_care": "baby_care",
    "hw_tools": "tool_hardware", "hw_device": "tool_device",
}


def spec_template_for(category_id: str | None) -> list[dict]:
    """按二级类目取规格模板；未命中回落到一级类目 / 默认模板。

    末尾再按一级类目追加「型号 / 成色 / 重量」这类通用维度（都是推荐项，可删可改）。
    """
    if not category_id:
        return DEFAULT_SPEC_TEMPLATE
    key = _SPEC_TEMPLATE_ALIAS.get(category_id, category_id)
    if key in SPEC_TEMPLATES:
        base = SPEC_TEMPLATES[key]
    else:
        parent = category_id.split("_", 1)[0]
        base = SPEC_TEMPLATES.get(_SPEC_TEMPLATE_ALIAS.get(parent, parent), DEFAULT_SPEC_TEMPLATE)
    extra = EXTRA_SPEC_DIMS.get(category_id.split("_", 1)[0], [])
    return list(base) + [{"name": d["name"], "values": list(d["values"])} for d in extra]


# ---- 物流 / 售后选项 ----
# 运费模板候选：优先读渔枢系统「货源 · 运费模板」那份规则（见 hub_freight_templates()），
# 读不到（独立部署 / 还没维护）时用下面这份兜底
FREIGHT_TEMPLATES: list[str] = [
    "全国包邮",
    "固定运费 ¥6",
    "固定运费 ¥10",
    "偏远地区加价",
    "运费到付",
]

# 闲鱼发货方式（四种），与渔枢系统同一口径
SHIPPING_CHOICES: list[tuple[str, str]] = [
    ("free", "邮寄：包邮"),
    ("distance", "邮寄：不包邮 - 按距离付费"),
    ("fixed", "邮寄：不包邮 - 固定收费"),
    ("self_pickup", "无需邮寄：买家自提"),
]
SHIPPING_LABELS = dict(SHIPPING_CHOICES)
# 老数据兼容（历史商品用过 fee = 按件运费）
SHIPPING_LEGACY_LABELS = {"fee": "邮寄：不包邮 - 固定收费"}


def shipping_mode_label(value: str | None) -> str:
    """发货方式取值 → 文案（兼容历史值）。"""
    if not value:
        return "—"
    return SHIPPING_LABELS.get(value) or SHIPPING_LEGACY_LABELS.get(value) or str(value)


def hub_freight_templates() -> list[str]:
    """读渔枢系统「货源 · 运费模板」里的模板名（读不到就用内置兜底，保证下拉不为空）。"""
    names: list[str] = []
    scope = _session_scope()
    if scope is not None:
        try:
            from app.services import shipping as hub_shipping  # noqa: PLC0415

            with scope() as db:
                names = hub_shipping.freight_names(db)
        except Exception:  # noqa: BLE001  独立部署 / 表还没建时用兜底
            names = []
    return names or list(FREIGHT_TEMPLATES)

AFTER_SALE_OPTIONS: list[str] = [
    "7天无理由退换",
    "15天无理由退换",
    # 渔枢端（2026-10-16 起）新建商品的默认口径就是这一条，同步过来要能选得到，
    # 否则在渔货栈这边编辑保存时下拉框认不出这个值，会把售后政策清成空。
    "非质量问题不退不换",
    "非质量问题不退换",
    "只换不修",
]

# 自营商品默认成本毛利提示（表单实时预览用）
DEFAULT_MARGIN_HINT = 0.30

__all__ = [
    "CATEGORY_TREE",
    "CATEGORY_NAME",
    "CATEGORY_PATH",
    "SPEC_TEMPLATES",
    "DEFAULT_SPEC_TEMPLATE",
    "spec_template_for",
    "FREIGHT_TEMPLATES",
    "AFTER_SALE_OPTIONS",
    "DEFAULT_MARGIN_HINT",
    # 货源广场类目（1688 风格两级类目 + 素材）
    "SOURCING_CATEGORY_TREE",
    "SOURCING_CATEGORY_FALLBACK",
    "SOURCING_CATEGORY_FALLBACK_HINT",
    "PURCHASE_NODES",
    "purchase_node_windows",
    "hub_node_items",
    # 类目唯一数据源：读渔枢系统「货源 · 类目管理」的那张表，读不到回落常量
    "hub_category_rows",
    "hub_category_source",
    "sourcing_tree",
    "sourcing_leaves",
    "category_tree",
    "category_path",
    "sourcing_leaf_index",
    "sourcing_sub_leaves",
    "sourcing_leaf_names_by_sub",
    "sourcing_l3_owner",
    "supplier_display_name",
    "SOURCING_SUB_LEAVES",
    "sourcing_match",
    "sourcing_leaf_of",
]
