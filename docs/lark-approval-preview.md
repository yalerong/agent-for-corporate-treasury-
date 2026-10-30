# Lark 原生审批 Preview 排障与 Base 权限

本文归档原 `cash-ops` 仓库中仍有独立价值的 Lark Approval v4 排障经验。调拨计算已由本仓库的 `cashflow/advisor.py` 及相关模块承接；这里只保留审批接入边界与实测结论。

## 结论

`approval/v4/instances/preview` 返回 `1395001` 时，不能直接认定是某一个表单字段格式错误。应先用一张近期、实际已审批通过的实例的完整原始 `form` 做对照验证，并要求 Lark 技术支持按 Request ID 查询服务端日志。

应用即使已经发布 `bitable:app:readonly` API 权限，也不代表其应用身份能够读取指定 Base。Base 所有者或管理员还必须把该自建应用以只读方式加入 Base。

## Base 只读授权

1. 启用并发布应用的 `bitable:app:readonly` 权限。
2. 由 Base 所有者或管理员通过“分享 → 邀请协作者”把自建应用添加为查看者；如果协作者选择器中搜不到应用，则通过 Base 的高级权限或数据权限设置添加。
3. 不授予编辑、导出、复制或权限管理能力。
4. 在调用 `approval.v4.instance.preview` 前，先验证应用身份确实能够读取关联记录。

操作人可能没有 Base 协作者管理权限；此时必须由 Base 所有者完成第 2 步。

## 已验证的事实

1. Base 页面协作者的查看权限不代表应用身份 API 可读。应用既需要 `bitable:app:readonly`，也需要 Base 侧的只读数据访问授权。
2. Base 应用身份读取成功后，关联记录仍必须按审批模板当前定义的控件格式传入；Base 记录 ID、页面展示文字和审批控件 `option.key` 不可假定为同一值。
3. 原生 Approval v4 与第三方或托管审批是不同能力。原生 preview 的排查不要求实现 `UISchema`、`UIData` 或外部回调接口。
4. 一份历史上实际审批通过的完整原始 `form`，以 `open_id` 和 `user_id` 两种身份分别调用 preview，仍返回 `1395001`。因此问题可能是审批定义在实例创建后发生变更、模板存在 API 兼容性问题，或 preview 服务端异常。
5. 历史实例中的 `department_id` 回填后返回 `99992357`；它不是当前 API 可接受的 `open_department_id`，不能作为创建或预览时的部门参数来源。

## 安全调用顺序

```text
确认 Base 应用身份只读访问
  -> 读取关联记录（只读）
  -> 用当前定义验证完整 form
  -> preview
  -> 人工确认
  -> create
```

任一 preview 失败，立即停止：不创建、不提交、不批准、不撤销审批。

## 给技术支持的材料

每次工单应包含：

- 审批定义 `approval_code`。
- 调用时间、`user_id_type` 和是否传入部门参数。
- 触发失败的完整 `form`，保留所有控件的 `id`、`name`、`type`、`value`、`option` 及 `fieldList` 嵌套结构。
- Request ID。
- 不包含 App Secret、tenant access token 或未脱敏的导出文件。

要求支持明确回答：当前审批定义的控件 option key、关联记录控件的传参结构、是否发生过模板版本或流程配置变更，以及对应 Request ID 的服务端失败原因。

## 不能再踩的坑

- 不要将浏览器“可查看”误判为应用 API 已授权。
- 不要用手工构造、未验证的 JSON 当作可提交表单。
- 不要把历史表单内的部门 ID 当作开放接口部门 ID。
- 不要因 `1395001` 而跳过 preview 直接 create。
- 不要向客服发送令牌或密钥；Request ID 加完整表单结构已足够让其复现与查日志。
