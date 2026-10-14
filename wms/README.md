# 货品检测入库出库管理系统（检测仓 · WMS）

**这是一个独立业务系统**，不是渔枢系统的一个新菜单。

## 边界

| 维度 | 做法 |
|---|---|
| 代码 | 顶层独立包 `wms/`，不 `import app.*` 的业务模块，不读平台的 `settings` |
| 数据 | 自己的 `Base`（`wms/db.py`）、自己的会话、自己的表（`wms_*`），平台的 `init_db` 不管也不该管它们 |
| 菜单 | 自己的侧栏（`wms/nav.py`），平台侧栏里只留一个「货品检测仓（独立系统）」跳转入口 |
| 权限 | 自己的角色权限（`wms/permissions.py` + `wms_roles/wms_role_perms/wms_user_roles`），跟平台的角色勾选体系是两套东西 |
| 页面 | 自己的 Jinja 环境、自己的 `base.html`，不继承平台的布局 |
| 互通 | 只走 `wms/bridge.py`：登录身份（平台的登录态）+ 平台账号表（只读挑人）。桥断了子系统的浅功能降级，不影响平台 |

平台的账号在这里**不等于权限**：平台管理员自动拥有检测仓全部权限，其余账号必须在
检测仓的「人员与授权」里分过角色才有权限，没权限打开页面一律跳 `/wms/denied` 说明缺哪个权限。

## 部署形态

- **合并部署**（默认）：由渔枢系统的 `app/main.py` 挂载，`app.include_router` + `install(app)`，同一端口；
- **独立部署**：`WMS_DATABASE_URL=… python -m wms.main`（默认 8100 端口）。登录由网关透传
  `X-Forwarded-User` / `X-Forwarded-User-Id`，`WMS_STANDALONE_ADMIN` 指定管理员。

两者共用同一份业务逻辑与模板，行为一致。

## 目录

```
wms/
  config.py      自己的配置（WMS_DATABASE_URL / WMS_BRAND / 阈值）
  db.py          自己的 Base、引擎、会话、init_tables()
  timeutil.py    自己的时间口径（库里 UTC naive，展示按用户时区）
  models.py      20 张业务表 + 3 张权限表
  nav.py         自己的菜单 + 「这条路径要什么权限」守卫表 + 徽标
  permissions.py 权限清单、内置角色、人员授权
  auth.py        操作员 = 平台登录身份 + 检测仓角色
  bridge.py      ↔ 平台互通桥（身份 / 账号），单向下依赖，可断
  web/           自己的 Jinja 环境、路由守卫、页面路由
  templates/     自己的布局与页面（base.html 是自己的壳）
  startup.py     启动动作：建表 → 内置角色 → 规则播种 → 报修超时打标
  main.py        独立部署入口
```

业务模块：`state / grading / ledger / intake / inspection / labeling / outbound / repairs / parts / stocktake / reports / seed / audit`。

回归：`python -m unittest tests.test_wms`（25 条，含「平台菜单里没有检测仓深链」「平台子账号不等于检测仓权限」两条独立性断言）。
