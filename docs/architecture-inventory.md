# 架构清单说明

自动生成清单：`docs/architecture-inventory.json`

- 生成日期：2026-10-05
- 分支：`feat/warehouse-mvp`
- 内容：`app/main.py` 路由声明、`app/db.py` ORM 模型、app 内部导入关系、未跟踪文件分类
- 用途：边界重构和发布候选审查的输入，不是稳定 API 文档，也不代表文件已纳入 Git

重新生成方式：

```bash
python scripts/inventory_architecture.py
```

当前清单基于一次性盘点脚本生成；后续应将脚本纳入可重复的工程工具，而不是手工维护 JSON。
