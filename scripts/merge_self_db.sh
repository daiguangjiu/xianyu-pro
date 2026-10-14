#!/usr/bin/env bash
# 自营端（渔货栈）的库 → HUB 平台端的库：两端合并成「一套系统、一套库」（2026-09-30）。
#
# 在服务器上跑：  bash scripts/merge_self_db.sh
#
# 跑之前的两件事：
#   1. 已经备份过（/root/backup/merge-YYYYMMDD/self.sql）；
#   2. HUB 应用已用新代码重启过一次 —— 自营端的表由 app/db.py::init_db 建在
#      HUB 库里，脚本只搬数据，不建表。
#
# 三块数据的处理不一样：
#   - 13 张自营端专属表：原样搬（表名、id 都不变）；
#   - shops（闲鱼授权凭证）：改名 shop_credentials 再搬 —— HUB 自己的 shops 是
#     「店铺档案」，语义不同，不能并；
#   - users：并进 HUB 的账号表 —— 手机号当用户名，bcrypt 老密码放进
#     legacy_password_hash（第一次登录时自动升级，老用户不用改密码），
#     platform_admin → admin，其余 → user。老 id 统一加偏移，避免撞 id。
set -euo pipefail

SELF_DB_CONTAINER="${SELF_DB_CONTAINER:-scx-postgres}"
SELF_DB_USER="${SELF_DB_USER:-supply_chain}"
SELF_DB_NAME="${SELF_DB_NAME:-supply_chain_xiaoyu}"
HUB_DB_CONTAINER="${HUB_DB_CONTAINER:-deploy-pkg_20260924232635-db-1}"
HUB_DB_USER="${HUB_DB_USER:-orderhub}"
HUB_DB_NAME="${HUB_DB_NAME:-orderhub}"

OUT="/tmp/merge_self_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"

say() { printf '\n==> %s\n' "$*"; }
hub_psql() { docker exec "$HUB_DB_CONTAINER" psql -U "$HUB_DB_USER" -d "$HUB_DB_NAME" "$@"; }

# 直接搬的表（两端不重名）。顺序按依赖排：被引用的表先搬（商家 → 商品 → 铺货 / 订单），
# 否则子表先插会因为外键指向还没搬的父表而整批回滚。
TABLES="merchants price_rules supplier_items sku_mappings channel_listings downstream_orders purchase_orders settlement_bills merchant_wallets merchant_applies merchant_users withdraw_applies exception_logs"

say "0/4 计算账号 id 偏移（自营端老账号要插进 HUB 的 users 表，id 不能撞）"
MAX_ID="$(hub_psql -tAc "SELECT COALESCE(MAX(id),0) FROM users" | tr -d '[:space:]')"
OFFSET=$((MAX_ID + 1000))
echo "HUB 现有账号最大 id=$MAX_ID，自营端账号 id 统一 +$OFFSET 后插入"

say "1/4 导出自营端数据"
for t in $TABLES; do
  docker exec "$SELF_DB_CONTAINER" pg_dump -U "$SELF_DB_USER" -d "$SELF_DB_NAME" \
    --data-only --table="$t" >"$OUT/$t.sql"
  echo "  导出 $t（$(wc -l <"$OUT/$t.sql") 行）"
done
docker exec "$SELF_DB_CONTAINER" pg_dump -U "$SELF_DB_USER" -d "$SELF_DB_NAME" \
  --data-only --table=shops |
  sed 's/public\.shops/public.shop_credentials/g' >"$OUT/shop_credentials.sql"
echo "  导出 shops → shop_credentials（$(wc -l <"$OUT/shop_credentials.sql") 行）"

say "2/4 生成账号合并 SQL（手机号当用户名，老密码走 legacy 自动升级）"
# 同一个手机号可能已经在 HUB 库里有账号（比如之前启动兜底建过），那就补齐而不是跳过：
# ON CONFLICT (username) DO UPDATE —— 手机号、老密码、角色、端权限都补上去，脚本可反复跑。
docker exec "$SELF_DB_CONTAINER" psql -U "$SELF_DB_USER" -d "$SELF_DB_NAME" -tAc "
SELECT format('INSERT INTO users (id, username, phone, password_hash, legacy_password_hash, display_name, role, portals, status, created_at, updated_at) VALUES (%s, %L, %L, %L, %L, %L, %L, %L, %L, now(), now()) ON CONFLICT (username) DO UPDATE SET phone = EXCLUDED.phone, legacy_password_hash = EXCLUDED.legacy_password_hash, display_name = EXCLUDED.display_name, role = EXCLUDED.role, portals = EXCLUDED.portals;',
  id + $OFFSET, phone, phone, '', password_hash, COALESCE(nickname, phone),
  CASE WHEN role = 'platform_admin' THEN 'admin' ELSE 'user' END,
  CASE WHEN role = 'platform_admin' THEN 'hub,self' ELSE 'self' END,
  COALESCE(status, 'active'))
FROM users ORDER BY id;" >"$OUT/users_merge.sql"
echo "  生成 $(wc -l <"$OUT/users_merge.sql") 条账号"

say "3/4 导入到 HUB 库（先清掉自营端这些表，脚本重跑也不会重复）"
# 老数据里有些列是空的（比如 supplier_items.shipping_fee 是后来加的列，两千多条历史
# 数据都没值），而 HUB 库这边按模型建的表是非空 —— 直接导会被 NOT NULL 拦下、整批回滚。
# 所以先把这些列放开：只放宽「源库允许空、目标库不允许空」的列，别的约束不动。
IN_LIST="'$(echo "$TABLES shop_credentials" | sed "s/ /','/g")'"
docker exec "$SELF_DB_CONTAINER" psql -U "$SELF_DB_USER" -d "$SELF_DB_NAME" -tAc \
  "SELECT table_name || '|' || column_name FROM information_schema.columns WHERE table_schema = 'public' AND is_nullable = 'YES' AND table_name IN ($IN_LIST) ORDER BY 1" |
  sort >"$OUT/self_nullable.txt"
hub_psql -tAc \
  "SELECT table_name || '|' || column_name FROM information_schema.columns WHERE table_schema = 'public' AND is_nullable = 'NO' AND table_name IN ($IN_LIST) ORDER BY 1" |
  sort >"$OUT/hub_notnull.txt"
comm -12 "$OUT/self_nullable.txt" "$OUT/hub_notnull.txt" >"$OUT/relax.txt"
while IFS='|' read -r t c; do
  [ -z "${t:-}" ] && continue
  hub_psql -q -c "ALTER TABLE \"$t\" ALTER COLUMN \"$c\" DROP NOT NULL"
done <"$OUT/relax.txt"
echo "  放宽 $(wc -l <"$OUT/relax.txt" | tr -d ' ') 个列的非空约束（历史数据里这些列本来就是空的）"
for t in $TABLES shop_credentials; do
  hub_psql -q -c "TRUNCATE TABLE $t RESTART IDENTITY CASCADE"
done
for t in $TABLES shop_credentials; do
  docker exec -i "$HUB_DB_CONTAINER" psql -U "$HUB_DB_USER" -d "$HUB_DB_NAME" -q <"$OUT/$t.sql"
  echo "  导入 $t"
done
docker exec -i "$HUB_DB_CONTAINER" psql -U "$HUB_DB_USER" -d "$HUB_DB_NAME" -q <"$OUT/users_merge.sql"
echo "  导入自营端账号"

say "4/4 修正账号外键（老 user_id → 合并后的新 id）"
# 注意：不能简单给 user_id 加偏移 —— 老账号可能早就以别的新 id 存在于 HUB 库里
# （比如启动兜底建过），这时 UPSERT 走的是更新、id 没变，加偏移就会指空。
# 所以这里按「老 id → 手机号 → 库里实际的 id」建一张临时映射表，再照它改子表。
docker exec "$SELF_DB_CONTAINER" psql -U "$SELF_DB_USER" -d "$SELF_DB_NAME" -tAc \
  "SELECT id || '|' || phone FROM users ORDER BY id" >"$OUT/user_ids.txt"
hub_psql -q -c "DROP TABLE IF EXISTS merge_user_map"
hub_psql -q -c "CREATE TABLE merge_user_map (old_id int PRIMARY KEY, new_id int)"
while IFS='|' read -r old_id phone; do
  [ -z "${old_id:-}" ] && continue
  hub_psql -q -c \
    "INSERT INTO merge_user_map SELECT $old_id, id FROM users WHERE username = '$phone' ON CONFLICT DO NOTHING"
done <"$OUT/user_ids.txt"
echo "  映射 $(hub_psql -tAc 'SELECT count(*) FROM merge_user_map' | tr -d '[:space:]') 个账号"
for t in merchant_users merchant_applies; do
  hub_psql -q -c \
    "UPDATE $t t SET user_id = m.new_id FROM merge_user_map m WHERE t.user_id = m.old_id AND m.new_id <> m.old_id"
  echo "  修正 $t.user_id"
done
hub_psql -q -c "DROP TABLE merge_user_map"
# 手工指定 id 插进去之后序列还停在旧值，不拨上去下次建号会撞主键
hub_psql -q -c "SELECT setval('users_id_seq', (SELECT COALESCE(MAX(id),0) FROM users))"

say "完成，核对一下："
hub_psql -c "SELECT count(*) AS 账号数 FROM users"
hub_psql -c "SELECT count(*) AS 自营商品数 FROM supplier_items"
hub_psql -c "SELECT count(*) AS 商家数 FROM merchants"
hub_psql -c "SELECT count(*) AS 授权店铺数 FROM shop_credentials"
echo "中间文件在：$OUT（核对完可以删）"
