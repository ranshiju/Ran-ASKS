# 可独立演进的建议型审阅者

每个直接子目录包含 `reviewer.json`、`instructions.md`、`feedback.schema.json`。新增角色通过受控 ID 发现，不新增 canonical function、Route profile 或调度分支。目录只保存可公开定义和合成材料，项目私有配置不放这里。

定义、专属反馈版本各自递增；prepare 冻结实际使用的三份资产，不向生产审阅者提供评估答案。公共协议及操作方式见 `operations/REVIEW.md`。CLI 的 `reviewers <id>` 同时返回定义和专属结构。

反馈 schema 使用 `review-local-schema-v1` 有界子集，不宣称支持完整 JSON Schema：type、properties、required、additionalProperties（布尔）、items、minLength/maxLength、minItems/maxItems、enum、description。不支持引用、正则、条件组合或执行代码；未知关键字在准备任务前拒绝。
