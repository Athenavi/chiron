# market/

内置技能资产（`skills/{name}/SKILL.md` 目录型技能）。

运行时由 `python-engine/app/skill/skillmd.py` 的 `SkillStore` 以 `scope=builtin` 读取：
本地开发从仓库根解析，容器部署必须只读挂载（compose 已配置）。

> **即将重构**：目录结构与资产组织方式在后续版本会调整（技能来源、版本化、
> 与插件能力的边界）。当前形态可继续使用，但新增资产请先确认归属。
