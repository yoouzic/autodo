# 桌面工作区汇总目录

可在 `config.local.json` 的 `workspace_discovery.collections` 中声明按执行器或类别组织的项目目录。AuToDo 使用这些目录生成工作区选项，文件保留原路径。

## 识别与展示

只枚举汇总根和显式分类的直接子目录，其余目录视为项目后停止深入。嵌套分类会补齐父分类；汇总根或分类本身不作为派工目标。即使汇总根带 `.git`，也不改变用户指定的汇总角色。文档/课程项目不必含 Git 或代码标记。


例如 `D:/projects/workspace_codex/courses/example` 会出现在 `Codex / courses` 组中。与最近会话或配置仓库匹配的同一路径会合并。

## 配置示例

```json
{
  "workspace_discovery": {
    "native_agents": true,
    "collections": [
      {
        "path": "D:/projects/workspace_codex",
        "label": "Codex",
        "agent": "codex",
        "groups": ["courses", "competitions"],
        "exclude": ["competitions/backups"]
      }
    ]
  }
}
```

`groups` 和 `exclude` 使用根内相对路径，拒绝逃逸、UNC、链接/重解析点和敏感账户根；集合根不能相互重叠，展示标签也不能重复。只读枚举有条数预算（每根 500 个目录入口、全局 2,000 个枚举条目），超限或不可读时报告提示；不会因此将尚未扫描的分类根降级为可修改项目。`native_agents: false` 仅关闭最近会话索引，显式配置的桌面汇总继续有效。

执行时仍执行原来的占用锁、目录边界检查、独立验收快照和本人复审流程。此功能改变发现与展示，不重新派发任务，也不改变各 agent 的模型配置。
