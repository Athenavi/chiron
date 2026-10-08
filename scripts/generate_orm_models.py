"""
生成 SQLAlchemy ORM 模型文件
从 configs/orm/V1/models.yaml 读取模型定义，使用 Jinja2 模板生成
输出到 shared/models/ 目录
"""

import sys
from datetime import datetime
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 路径常量
MODELS_YAML = PROJECT_ROOT / "configs" / "orm" / "V1" / "models.yaml"
TEMPLATES_DIR = PROJECT_ROOT / "scripts" / "templates"
OUTPUT_DIR = PROJECT_ROOT / "shared" / "models"


def camel_to_underscore(name: str) -> str:
    """驼峰转下划线"""
    import re
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    s2 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1)
    result = s2.lower()
    if not result.endswith('s'):
        result += 's'
    return result


def model_name_to_filename(name: str) -> str:
    """模型名转文件名: MediaAsset -> media_asset"""
    import re
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    s2 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1)
    return s2.lower()


# SQLAlchemy 保留字列表
SQLALCHEMY_RESERVED_NAMES = {'metadata', 'query', 'session'}


def convert_properties_to_fields(properties: dict, model_name: str, all_models: dict) -> dict:
    """将 properties 转换为字段定义"""
    fields = {}
    for field_name, prop in properties.items():
        # 处理保留字：自动重命名 metadata -> metadata_data，但保持数据库列名为原始名称
        python_name = prop.get('python_name', field_name)
        db_column = prop.get('db_column', '')
        if python_name in SQLALCHEMY_RESERVED_NAMES:
            # Python 属性名使用别名
            python_name = f'{python_name}_data'
            # 数据库列名保持原始名称（如果没有显式指定 db_column）
            if not db_column:
                db_column = field_name

        # 归一：models.yaml 用 `type: string, format: date-time` 表达时间戳（时间语义），
        # 需转成 datetime，否则模板会落到 String(255) 分支——库中时间戳列被建成
        # character varying，与 Go 侧的 time.Time 不兼容（见迁移 8b3f1d60a7c2 的背景）。
        # 与 scripts/cli/commands/generate_models.py 的同类处理保持一致。
        field_type = prop.get('type', 'string')
        if field_type == 'string' and prop.get('format') == 'date-time':
            field_type = 'datetime'

        # 处理 default: now() -> datetime.utcnow (仅对 datetime 类型)
        default_value = prop.get('default')
        if default_value == 'now()' and field_type in ('datetime', 'timestamp'):
            default_value = 'datetime.utcnow'

        field = {
            'type': field_type,
            'nullable': prop.get('nullable', False),
            'primary_key': prop.get('primary_key', False),
            'unique': prop.get('unique', False),
            'index': prop.get('index', False),
            'default': default_value,
            'max_length': prop.get('maxLength', prop.get('max_length', 255)),
            'description': prop.get('description', ''),
            'db_column': db_column,
            'python_name': python_name,
            'doc': prop.get('description', field_name),
            'autoincrement': prop.get('autoincrement', True),
        }
        if prop.get('foreign_key', False):
            field['foreign_key'] = True
            field['fk_table'] = prop.get('fk_table', '')
            field['fk_column'] = prop.get('fk_column', 'id')
        if prop.get('db_type') == 'bigint':
            field['type'] = 'bigint'
        fields[field_name] = field
    return fields


def check_decimal_fields(fields: dict) -> bool:
    return any(f.get('type') == 'decimal' for f in fields.values())


def check_text_fields(fields: dict) -> bool:
    return any(f.get('type') == 'text' for f in fields.values())


def check_timestamp_fields(fields: dict) -> bool:
    return any(f.get('type') in ('datetime', 'timestamp') for f in fields.values())


def check_json_fields(fields: dict) -> bool:
    return any(f.get('type') == 'object' for f in fields.values())


def check_jsonb_fields(fields: dict) -> bool:
    return any(f.get('db_type') == 'jsonb' for f in fields.values())


def check_foreign_keys_in_fields(fields: dict) -> bool:
    return any(f.get('foreign_key', False) for f in fields.values())


def check_relationships(model_def: dict) -> bool:
    return bool(model_def.get('relationships'))


def generate_all():
    """生成所有 ORM 模型文件"""
    print("=" * 60)
    print("生成 SQLAlchemy ORM 模型文件")
    print("=" * 60)

    # 加载 models.yaml
    if not MODELS_YAML.exists():
        print(f"❌ models.yaml 不存在: {MODELS_YAML}")
        return

    with open(MODELS_YAML, encoding='utf-8') as f:
        data = yaml.safe_load(f)

    orm_models = {name: defn for name, defn in data.get('models', {}).items() if defn.get('orm') is True}
    print(f"📦 模型配置: {len(orm_models)} 个 ORM 模型\n")

    if not orm_models:
        print("⚠️ 没有需要生成的 ORM 模型")
        return

    # 设置 Jinja2 环境
    jinja_env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        trim_blocks=True,
        lstrip_blocks=True
    )
    jinja_env.filters['quote'] = lambda x: f"'{x}'"
    jinja_env.filters['camel_to_underscore'] = camel_to_underscore

    # 确保输出目录存在
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    generated_count = 0
    lazy_imports = {}

    for model_name, model_def in orm_models.items():
        try:
            properties = model_def.get('properties', {})
            fields = convert_properties_to_fields(properties, model_name, orm_models)

            # 检测 uuid 主键 / datetime 默认值
            has_uuid_pk = False
            has_datetime_default = False
            for fdef in fields.values():
                if fdef.get('primary_key') and fdef.get('type') == 'string':
                    has_uuid_pk = True
                if fdef.get('default') == 'datetime.utcnow':
                    has_datetime_default = True

            class_def = {
                'fields': fields,
                'table': model_def.get('table'),
                'description': model_def.get('description', ''),
                'relationships': model_def.get('relationships', {}),
                'indexes': model_def.get('indexes', []),
                'unique_constraints': model_def.get('unique_constraints', []),
            }

            template_data = {
                'model_name': model_name,
                'classes': {model_name: class_def},
                'has_json': check_json_fields(fields),
                'has_jsonb': check_jsonb_fields(fields),
                'table_prefix': '',
                'all_models': orm_models,
                'generation_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'has_numeric': check_decimal_fields(fields),
                'has_decimal': check_decimal_fields(fields),
                'has_text': check_text_fields(fields),
                'has_timestamps': check_timestamp_fields(fields),
                'has_foreign_keys': check_foreign_keys_in_fields(fields),
                'has_relationships': check_relationships(model_def),
                'is_unlogged': model_def.get('unlogged', False),
                'custom_methods': {},
                'table_has_indexes': bool(model_def.get('indexes')),
                'table_has_unique_constraints': bool(model_def.get('unique_constraints')),
                'ns': {
                    'has_uuid_pk': has_uuid_pk,
                    'has_datetime_default': has_datetime_default,
                },
                'module_path': '',
            }

            template = jinja_env.get_template('orm-template.jinja2')
            content = template.render(**template_data)

            output_filename = f"{model_name_to_filename(model_name)}.py"
            output_path = OUTPUT_DIR / output_filename

            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(content)

            # 注册到 lazy_imports
            module_name = output_filename.replace('.py', '')
            lazy_imports[model_name] = f".{module_name}"

            print(f"  ✅ {model_name:30s} -> {output_path.name}")
            generated_count += 1

        except Exception as e:
            import traceback
            print(f"  ❌ {model_name}: {e}")
            traceback.print_exc()

    # 更新 __init__.py 中的 _LAZY_IMPORTS
    update_init(lazy_imports)

    print(f"\n{'=' * 60}")
    print(f"✅ 共生成 {generated_count} 个模型文件")
    print(f"📁 输出目录: {OUTPUT_DIR}")
    print(f"{'=' * 60}")


def update_init(lazy_imports: dict[str, str]):
    """更新 __init__.py 的 _LAZY_IMPORTS"""
    init_path = OUTPUT_DIR / '__init__.py'


    # 构建新的 _LAZY_IMPORTS 部分
    imports_lines = []
    for model_name in sorted(lazy_imports.keys()):
        module_path = lazy_imports[model_name]
        imports_lines.append(f"    '{model_name}': '{module_path}',")

    imports_str = '\n'.join(imports_lines)

    new_content = f'''"""
SQLAlchemy 模型包 - 由代码生成器自动生成
所有模型统一继承 Base，由 Alembic 自动发现
"""

from .base import Base

# _LAZY_IMPORTS 字典由代码生成器维护，用于 Alembic 自动发现模型
# 格式: {{ModelName: .module_path}}
_LAZY_IMPORTS = {{
{imports_str}
}}
'''

    with open(init_path, 'w', encoding='utf-8') as f:
        f.write(new_content)

    print(f"  📝 更新 __init__.py ({len(lazy_imports)} 个懒加载导入)")


if __name__ == '__main__':
    generate_all()
