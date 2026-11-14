# market/

技能资产目录。**本仓库不随附任何技能资产** —— `skills/` 下只保留一个占位文件，用于让
compose 的只读挂载（`./market/skills:/srv/chiron/builtin-skills:ro`）指向一个存在的路径。

## 自带技能

放入目录型技能即可被引擎发现，无额外配置：

```
market/skills/<技能名>/SKILL.md   # frontmatter（name 必须等于目录名）+ 正文
```

运行时由 `python-engine/app/skill/skillmd.py` 的 `SkillStore` 以 `scope=builtin` 读取：
本地开发从仓库根解析，容器部署经上面的只读挂载 + `CHIRON_BUILTIN_SKILLS_PATH` 提供。
未被挂载时只告警、不报错，技能列表不含内置技能。

格式约束（`description ≤1024`、单文件 ≤10MB、`name` 必须等于目录名）与解析规则见该模块的
文档字符串。

> 第三方技能资产请确认其许可与出处后再放入，不要提交来源不明的 Skill。
