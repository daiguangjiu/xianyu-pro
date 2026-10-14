# 闲参谋身份同步接口

闲参谋后端在用户首次成功登录/完成入驻后调用平台身份同步接口。该接口只登记外部身份，不创建平台登录用户。平台仅在手机号精确匹配到现有 `users` 用户时建立关联；角色资料仅记录来源值，不授予平台角色或端权限。

## Endpoint

```http
POST /openapi/v1/identities/xh-fish/sync
Content-Type: application/json
X-Client-Id: xh-fish
X-Timestamp: <Unix 秒>
X-Nonce: <随机值>
X-Signature: <HMAC-SHA256 十六进制小写>
```

签名原文为以下字段以换行符连接：

```text
HTTP_METHOD\nPATH\nTIMESTAMP\nNONCE\nSHA256_HEX(原始请求体)
```

签名密钥通过平台 `XIANMOU_IDENTITY_SYNC_SECRET` 与闲参谋 `PLATFORM_IDENTITY_SYNC_SECRET` 配置；闲参谋还需配置 `PLATFORM_IDENTITY_SYNC_URL` 为平台服务地址。时间戳允许与平台时间相差最多 300 秒。生产环境必须通过 HTTPS 调用。

## Request

```json
{
  "external_user_id": "123",
  "phone": "13800000000",
  "display_name": "用户昵称",
  "source_role": "account"
}
```

`external_user_id` 必须使用闲参谋稳定用户 ID；`phone` 可空，仅用于匹配；`source_role` 是来源资料，不影响平台授权。

## Success response

```json
{
  "external_identity_id": 42,
  "linked": true
}
```

`linked=false` 表示手机号没有匹配到平台用户，外部身份已记录但未关联。

## Retry and errors

闲参谋登录成功不依赖同步成功；接口超时或暂时失败应在后续登录时重试。重复请求以 `source_system + external_user_id` 幂等更新。手机号命中多个用户或既有外部身份出现关联冲突时返回 `409`，不得静默改绑。未配置服务密钥时接口返回 `503`；鉴权失败返回 `401`，手机号格式错误返回 `422`。
