"""
使用方法:
    python scripts/generate_routes.py

子命令
    - generate-all: 生成所有代码（默认）
    - generate-model --model <ModelName> [<ModelName> ...]: 仅生成指定模型
    - sync-to-django [--app <app_name>]: 同步模型到 Django（部分功能）
"""

import argparse
import importlib.util
import inspect
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, TemplateNotFound

# ==================== 全局路径常量 ====================
current_version = "V1"

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "configs"
MODELS_YAML = CONFIG_DIR / "orm" / current_version / "models.yaml"
EXTRA_DEFS_DIR = CONFIG_DIR / "orm" / current_version / "extras"
TEMPLATES_DIR = PROJECT_ROOT / "scripts" / "templates"
SHARED_MODELS_DIR = CONFIG_DIR / "models"


class RouteGenerator:
    """路由代码生成器"""

    def __init__(self, config_path: str | None = None):
        """
        初始化生成器

        Args:
            config_path: routes.yaml 配置文件路径（可选，不提供则使用全局默认路径）
        """
        self.config = {}

        # 加载模型配置（models.yaml）
        self.models_config_path = MODELS_YAML
        self.extra_models = {}
        if self.models_config_path.exists():
            with open(self.models_config_path, encoding='utf-8') as f:
                extra_config = yaml.safe_load(f)
                self.extra_models = extra_config.get('models', {})
                print(f"已加载模型配置：{len(self.extra_models)} 个模型")

        # 设置模板环境
        self.jinja_env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            trim_blocks=True,
            lstrip_blocks=True
        )
        self.jinja_env.filters['quote'] = lambda x: f"'{x}'"

        # 注册自定义过滤器
        sys.path.insert(0, str(PROJECT_ROOT))
        from jinja_filters import register_filters
        register_filters(self.jinja_env)

        # 提取配置信息
        self.api_version = self.config.get('api_version', 'v1')
        self.base_path = self.config.get('base_path', f'/api/{self.api_version}')
        self.models = {**self.config.get('models', {}), **self.extra_models}
        self.endpoints = self.config.get('endpoints', [])
        self.generation_config = self.config.get('generation', {})

    def generate_all(self):
        """生成所有代码"""
        print("=" * 70)
        print("开始生成路由代码...")
        print("=" * 70)
        print(f"\n配置文件：{self.config_path}")
        print(f"端点数量：{len(self.endpoints)}")
        print(f"模型数量：{len(self.models)}")

        # 1. 生成 Shared SQLAlchemy Models
        self._generate_shared_models()

        # 2. 优化 SQLAlchemy 模型的导入
        self._optimize_sqlalchemy_imports()

        # 3. 自动格式化生成的文件
        self._format_generated_files()

        print("=" * 70)
        print("✅ 代码生成完成!")
        print("=" * 70)

    def _generate_shared_models(self):
        """生成 Shared SQLAlchemy 模型文件（从 models[*].orm 读取配置）"""
        print("\n[5/5] 生成 Shared SQLAlchemy Models...")

        try:
            from src.setting import settings
        except ImportError:
            settings = type('Settings', (), {'db_table_prefix': ''})()

        table_prefix = getattr(settings, 'db_table_prefix', '')

        # 收集所有需要生成 ORM 的模型（orm: true）
        orm_models = {name: defn for name, defn in self.models.items() if defn.get('orm') is True}
        if not orm_models:
            print("  ⚠️ 没有需要生成 ORM 的模型，跳过")
            return

        SHARED_MODELS_DIR.mkdir(parents=True, exist_ok=True)

        generated_count = 0
        success_models = []
        used_modules = set()

        for model_name, model_def in orm_models.items():
            try:
                # 获取模块路径（子目录）
                module_path = model_def.get('module', '').strip()
                if module_path:
                    module_path = module_path.replace('\\', '/').strip('/')
                    used_modules.add(module_path)
                    output_dir = SHARED_MODELS_DIR / module_path
                else:
                    output_dir = SHARED_MODELS_DIR
                output_dir.mkdir(parents=True, exist_ok=True)

                output_path = output_dir / f"{self._model_name_to_filename(model_name)}.py"
                self._generate_model_file(model_name, model_def, output_path, table_prefix)
                print(f"  [OK] Model: {output_path}")
                success_models.append(model_name)
                generated_count += 1

            except Exception as e:
                import traceback
                print(f"  [ERROR] 生成 {model_name} 失败：{e}")
                print(f"    详细信息：{traceback.format_exc()}")
                raise SystemExit(1) from None

        # 为每个使用的子模块创建 __init__.py
        self._create_module_init_files(SHARED_MODELS_DIR, used_modules)

        # 全部生成成功后才更新顶层 __init__.py
        if success_models:
            successful_orm_config = {k: v for k, v in orm_models.items() if k in success_models}
            self._update_shared_models_init_from_orm(successful_orm_config)

        print(f"  ✅ 共生成 {generated_count} 个模型文件")

    def generate_single_model(self, model_names: list[str]):
        """
        仅生成指定的单个或多个 ORM 模型文件（不重新生成全部模型）
        Args:
            model_names: 要生成的模型名称列表，如 ["TeamComment", "CustomPostContent"]
        """
        print(f"\n[单模型生成] 指定模型: {model_names}")

        try:
            from src.setting import settings
        except ImportError:
            settings = type('Settings', (), {'db_table_prefix': ''})()

        table_prefix = getattr(settings, 'db_table_prefix', '')

        SHARED_MODELS_DIR.mkdir(parents=True, exist_ok=True)

        generated_count = 0
        success_models = []
        used_modules = set()

        for model_name in model_names:
            model_def = self.models.get(model_name)
            if not model_def:
                print(f"  ⚠️ 模型 '{model_name}' 未在 models.yaml 中定义，跳过")
                continue
            if not model_def.get('orm'):
                print(f"  ⚠️ 模型 '{model_name}' 未启用 ORM (orm: true)，跳过")
                continue

            try:
                module_path = model_def.get('module', '').strip()
                if module_path:
                    module_path = module_path.replace('\\', '/').strip('/')
                    used_modules.add(module_path)
                    output_dir = SHARED_MODELS_DIR / module_path
                else:
                    output_dir = SHARED_MODELS_DIR
                output_dir.mkdir(parents=True, exist_ok=True)

                output_path = output_dir / f"{self._model_name_to_filename(model_name)}.py"
                self._generate_model_file(model_name, model_def, output_path, table_prefix)
                print(f"  [OK] {model_name}: {output_path}")
                generated_count += 1
                success_models.append(model_name)

            except Exception as e:
                print(f"  [FAIL] {model_name}: {e}")
                import traceback
                traceback.print_exc()

        # 更新 __init__.py
        if used_modules:
            self._create_module_init_files(SHARED_MODELS_DIR, used_modules)
        if success_models:
            successful_orm_config = {k: v for k, v in self.models.items() if k in success_models}
            self._update_shared_models_init_from_orm(successful_orm_config)

        print(f"  ✅ 共生成 {generated_count} 个模型文件")

    def _generate_model_file(self, model_name: str, model_def: dict, output_path: Path, table_prefix: str):
        """生成单个模型的 SQLAlchemy 文件（提取的公共逻辑）"""
        # 从 properties 自动生成 SQLAlchemy 字段
        properties = model_def.get('properties', {})
        fields = self._convert_properties_to_fields(
            properties,
            model_name=model_name,
            all_models=self.models,
            table_prefix=table_prefix
        )

        # 获取自定义方法
        def_list = model_def.get('def_list', [])
        custom_methods = {}
        if def_list:
            defs_target = model_def.get('defs_target', f"{model_name.lower()}_defs.py")
            custom_methods = self._load_custom_methods_from_target(model_name, def_list, defs_target)

        # 准备模板上下文
        class_def = {
            'fields': fields,
            'table': model_def.get('table'),
            'description': model_def.get('description'),
            'relationships': model_def.get('relationships', {}),
            'indexes': model_def.get('indexes', []),
            'unique_constraints': model_def.get('unique_constraints', []),
        }

        # 检测 uuid 主键 / datetime 默认值（用于控制顶层 import）
        has_uuid_pk = False
        has_datetime_default = False
        for fdef in fields.values():
            if fdef.get('primary_key') and fdef.get('type') == 'string':
                has_uuid_pk = True
            if fdef.get('default') == 'datetime.utcnow':
                has_datetime_default = True

        module_path = model_def.get('module', '').strip().replace('\\', '/').strip('/')

        template_data = {
            'model_name': model_name,
            'classes': {model_name: class_def},
            'table_prefix': table_prefix,
            'all_models': self.models,
            'generation_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'has_numeric': self._check_decimal_fields_from_properties(properties),
            'has_decimal': self._check_decimal_fields_from_properties(properties),
            'has_text': self._check_text_fields_from_properties(properties),
            'has_timestamps': self._check_timestamp_fields_from_properties(properties),
            'has_foreign_keys': self._check_foreign_keys_in_fields(fields),
            'has_relationships': self._check_relationships(model_def),
            'is_unlogged': model_def.get('unlogged', False),
            'custom_methods': custom_methods,
            'table_has_indexes': bool(model_def.get('indexes')),
            'table_has_unique_constraints': bool(model_def.get('unique_constraints')),
            'ns': {
                'has_uuid_pk': has_uuid_pk,
                'has_datetime_default': has_datetime_default,
            },
            'module_path': module_path,
        }

        content = self._render_template('sqlalchemy_model.py.jinja2', template_data)
        self._write_file(output_path, content)

    def _get_output_path(self, framework: str, config_key: str) -> Path | None:
        """获取输出文件路径（从 generation 配置读取）"""
        gen_config = self.generation_config.get(framework, {})
        if not gen_config:
            return None

        output_dir = PROJECT_ROOT / gen_config.get('output_dir', '')
        output_file = gen_config.get(config_key, '')

        if output_dir and output_file:
            output_dir.mkdir(parents=True, exist_ok=True)
            return output_dir / output_file

        return None

    def _filter_endpoints_by_module(self) -> dict[str, list[dict]]:
        """按模块分组端点"""
        modules = {}
        for endpoint in self.endpoints:
            module_name = endpoint.get('module', 'default')
            modules.setdefault(module_name, []).append(endpoint)
        return modules

    def _collect_django_imports(self) -> list[str]:
        """收集 Django Ninja 需要的导入"""
        imports = {
            "from ninja import Router, Form, Query, Path",
            "from django.http import HttpRequest",
            "from django_blog.django_ninja_compat import ApiResponse"
        }
        for endpoint in self.endpoints:
            params = endpoint.get('parameters', [])
            for param in params:
                location = param.get('location')
                if location in ('form', 'query', 'path'):
                    imports.add(f"from ninja import {location.capitalize()}")
        return sorted(imports)

    def _collect_fastapi_imports(self) -> list[str]:
        """收集 FastAPI 需要的导入"""
        imports = {
            "from fastapi import APIRouter, Depends, Form, Query, Path",
            "from src.api.v1.core.responses import ApiResponse"
        }
        for endpoint in self.endpoints:
            if endpoint.get('django_ninja_auth', False) or endpoint.get('fastapi_dependencies', []):
                imports.add("from src.auth import jwt_required_dependency as jwt_required")
        return sorted(imports)

    def _model_name_to_filename(self, model_name: str) -> str:
        """将模型名转换为文件名（驼峰转下划线）"""
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', model_name)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

    # ==================== 字段类型检查辅助方法 ====================

    @staticmethod
    def _check_fields_any(properties: dict, predicate) -> bool:
        """通用检查：properties 中是否存在满足 predicate 的字段"""
        return any(predicate(field_def) for field_def in properties.values())

    def _check_list_fields_from_properties(self, properties: dict) -> bool:
        return self._check_fields_any(properties, lambda f: f.get('type') == 'array')

    def _check_numeric_fields_from_properties(self, properties: dict) -> bool:
        return self._check_fields_any(properties, lambda f: f.get('type') in ('number', 'integer'))

    def _check_decimal_fields_from_properties(self, properties: dict) -> bool:
        return self._check_fields_any(properties, lambda f: f.get('type') in ('number', 'float', 'decimal'))

    def _check_text_fields_from_properties(self, properties: dict) -> bool:
        return self._check_fields_any(properties, lambda f: f.get('type') == 'string' and f.get('maxLength', 0) > 500)

    def _check_timestamp_fields_from_properties(self, properties: dict) -> bool:
        return self._check_fields_any(properties, lambda f: f.get('format') == 'date-time')

    def _check_foreign_keys_in_fields(self, fields: dict) -> bool:
        return any(field.get('foreign_key') for field in fields.values())

    def _check_relationships(self, model_def: dict) -> bool:
        return bool(model_def.get('relationships', {}))

    # ==================== ORM 字段转换 ====================

    def _convert_properties_to_fields(self, properties: dict, model_name: str = None,
                                      all_models: dict = None, table_prefix: str = '') -> dict:
        """从 API properties 转换为 SQLAlchemy fields，支持完整配置"""
        reserved_names = {
            'metadata', 'registry', 'declarative_base', 'Base',
            'query', 'session', 'mapper', 'column_property',
            'composite', 'synonym', 'relationship', 'backref',
            'validate', 'reconstructor', 'declared_attr',
            'hybrid_property', 'hybrid_method', 'AssociationProxy'
        }

        fields = {}
        has_id_field = any(k == 'id' for k in properties)

        for prop_name, prop_def in properties.items():
            raw_type = prop_def.get('type', 'string')
            field_type = self._map_property_type_to_sqlalchemy(raw_type)

            # 处理保留字
            python_field_name = prop_name
            db_column_name = None
            if prop_name.lower() in reserved_names:
                python_field_name = f"extra_{prop_name}"
                db_column_name = prop_name

            field_info = {
                'type': field_type,
                'description': prop_def.get('description', prop_name),
                'doc': prop_def.get('description', prop_name),
                'python_name': python_field_name,
                'db_column': db_column_name,
            }

            # 特殊处理：string + date-time -> datetime
            if raw_type == 'string' and prop_def.get('format') == 'date-time':
                field_info['type'] = 'datetime'

            # 主键
            if prop_name == 'id':
                field_info['primary_key'] = True
                field_info['autoincrement'] = True
            elif prop_def.get('primaryKey'):
                field_info['primary_key'] = True
                if not has_id_field and field_type == 'integer':
                    field_info['autoincrement'] = True

            # 常规属性
            if prop_def.get('nullable'):
                field_info['nullable'] = True
            if prop_def.get('maxLength'):
                field_info['max_length'] = prop_def['maxLength']
            if 'default' in prop_def:
                field_info['default'] = prop_def['default']
            if prop_def.get('unique'):
                field_info['unique'] = True
            if prop_def.get('index'):
                field_info['index'] = True
            if prop_def.get('sensitive'):
                field_info['sensitive'] = True

            # Decimal 精度
            if raw_type in ('number', 'float', 'decimal'):
                field_info['type'] = 'decimal'
                field_info['max_digits'] = prop_def.get('maxDigits', 10)
                field_info['decimal_places'] = prop_def.get('decimalPlaces', 2)

            # 外键
            if prop_def.get('foreignKey'):
                fk_model_name = prop_def['foreignKey']
                field_info['foreign_key'] = fk_model_name
                target_model = all_models.get(fk_model_name, {}) if all_models else {}
                target_table = target_model.get('table', self._model_name_to_filename(fk_model_name))
                field_info['fk_table'] = table_prefix + target_table
                field_info['fk_column'] = 'id'
                if model_name and fk_model_name == model_name:
                    field_info['is_self_reference'] = True

            fields[python_field_name] = field_info

        return fields

    def _map_property_type_to_sqlalchemy(self, prop_type: str) -> str:
        """映射 TypeScript/JSON 类型到 SQLAlchemy 类型"""
        mapping = {
            'integer': 'integer',
            'bigint': 'bigint',
            'number': 'decimal',
            'float': 'decimal',
            'string': 'string',
            'boolean': 'boolean',
            'array': 'string',
            'object': 'text',
            'text': 'text',
            'datetime': 'datetime',
            'timestamp': 'datetime',
            'date': 'datetime',
        }
        return mapping.get(prop_type, 'string')

    # ==================== 自定义方法加载 ====================

    def _load_custom_methods_from_target(self, model_name: str, def_list: list, defs_target: str) -> dict:
        """
        从指定的文件加载自定义方法
        Args:
            model_name: 模型名称（如 User）
            def_list: 方法名称列表（如 ['is_vip']）
            defs_target: 目标文件名（相对于 shared/defs/），如 'user_defs.py' 或 'mydef.py'
        Returns:
            包含方法源码的字典 {method_name: source_code}
        """
        custom_methods = {}
        if Path(defs_target).is_absolute():
            defs_file = Path(defs_target)
        else:
            defs_file = EXTRA_DEFS_DIR / defs_target

        if not defs_file.exists():
            print(f"  ⚠️ 警告：自定义方法文件不存在：{defs_file}")
            return custom_methods

        try:
            module_name = Path(defs_target).stem
            spec = importlib.util.spec_from_file_location(module_name, defs_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            for method_name in def_list:
                if hasattr(module, method_name):
                    method = getattr(module, method_name)
                    try:
                        source = inspect.getsource(method)
                        custom_methods[method_name] = source
                        print(f"  [OK] 加载自定义方法：{model_name}.{method_name} (from {defs_target})")
                    except Exception as e:
                        print(f"  ⚠️ 警告：无法获取方法 {method_name} 的源码：{e}")
                else:
                    print(f"  ⚠️ 警告：方法 {method_name} 在 {defs_target} 中未找到")
        except Exception as e:
            import traceback
            print(f"  [ERROR] 加载自定义方法失败：{e}")
            print(f"    详细信息：{traceback.format_exc()}")

        return custom_methods

    # ==================== 关联表扫描 ====================

    def _scan_association_tables(self) -> dict[str, list]:
        """
        自动扫描 shared/models 目录下的关联表（Table 对象）
        Returns:
            {'imports': [import_statements], 'exports': [export_names]}
        """
        imports = []
        exports = []

        if not SHARED_MODELS_DIR.exists():
            return {'imports': imports, 'exports': exports}

        for py_file in SHARED_MODELS_DIR.glob("*.py"):
            if py_file.name.startswith('_') or py_file.name == '__init__.py':
                continue
            try:
                content = py_file.read_text(encoding='utf-8')
                matches = re.findall(r'^(\w+)\s*=\s*Table\(', content, re.MULTILINE)
                if matches:
                    module_name = py_file.stem
                    for table_name in matches:
                        imports.append(f"from .{module_name} import {table_name}")
                        exports.append(table_name)
                        print(f"  [OK] 检测到关联表: {table_name} (from {module_name}.py)")
            except Exception as e:
                print(f"  [WARN] 扫描 {py_file.name} 失败: {e}")

        return {'imports': imports, 'exports': exports}

    def _create_module_init_files(self, output_base: Path, used_modules: set):
        """为每个使用的子模块目录创建 __init__.py 文件"""
        for module_path in sorted(used_modules):
            module_dir = output_base / module_path
            module_dir.mkdir(parents=True, exist_ok=True)
            init_file = module_dir / "__init__.py"

            imports = []
            exports = []
            for py_file in sorted(module_dir.iterdir()):
                if py_file.name.startswith('_') or py_file.suffix != '.py':
                    continue
                try:
                    content = py_file.read_text(encoding='utf-8')
                except (UnicodeDecodeError, OSError):
                    continue
                class_matches = re.findall(r'class\s+(\w+)\s*\([^)]*Base[^)]*\)\s*:', content)
                if class_matches:
                    module_name = py_file.stem
                    for class_name in class_matches:
                        imports.append(f"from .{module_name} import {class_name}")
                        exports.append(class_name)

            if imports:
                imports_section = "\n".join(imports)
                all_section = ", ".join(f"'{name}'" for name in sorted(exports))
                init_content = f'''"""
{module_path} 子模块 - 模型定义
由代码生成器自动生成 - 请勿手动修改
"""
{imports_section}

__all__ = [{all_section}]
'''
            else:
                init_content = f'''"""
{module_path} 子模块 - 模型定义
由代码生成器自动生成 - 请勿手动修改
"""
'''
            self._write_file(init_file, init_content)
            print(f"  [OK] Module __init__.py: {init_file} ({len(exports)} 个模型)")

    def _update_shared_models_init_from_orm(self, orm_models_config: dict):
        """更新 shared/models/__init__.py 文件（懒加载版本）"""
        init_path = SHARED_MODELS_DIR / "__init__.py"

        lazy_entries = []  # (model_name, module_path) 元组列表
        all_exports = ['Base']

        for model_name, model_def in orm_models_config.items():
            filename = self._model_name_to_filename(model_name)
            module_path = model_def.get('module', '').strip()
            if module_path:
                module_path = module_path.replace('\\', '/').strip('/')
                module_dotted = module_path.replace('/', '.')
                lazy_path = f'.{module_dotted}.{filename}'
            else:
                lazy_path = f'.{filename}'
            lazy_entries.append((model_name, lazy_path))
            all_exports.append(model_name)

        known_model_names = {name for name, _ in lazy_entries}

        # 扫描手动模型文件
        for py_file in sorted(SHARED_MODELS_DIR.rglob("*.py")):
            if py_file.name.startswith('_') or py_file.name == '__init__.py':
                continue
            try:
                content = py_file.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                continue
            if 'from . import Base' not in content.replace(' ', ''):
                continue
            class_matches = re.findall(r'class\s+(\w+)\s*\(\s*Base\s*\)\s*:', content)
            for class_name in class_matches:
                if class_name in known_model_names:
                    continue
                rel_path = py_file.relative_to(SHARED_MODELS_DIR)
                parts = list(rel_path.with_suffix('').parts)
                lazy_path = '.' + ('.'.join(parts) if len(parts) > 1 else parts[0])
                lazy_entries.append((class_name, lazy_path))
                all_exports.append(class_name)
                known_model_names.add(class_name)
                print(f"  [INFO] 发现手动模型文件：{rel_path} -> {class_name}")

        # 关联表
        association_imports = self._scan_association_tables()
        for import_stmt in association_imports['imports']:
            match = re.search(r'from \.(\S+) import (\w+)', import_stmt)
            if match:
                lazy_entries.append((match.group(2), f'.{match.group(1)}'))
                all_exports.append(match.group(2))

        lazy_lines = [f"    '{name}': '{path}'," for name, path in sorted(lazy_entries, key=lambda x: x[0])]
        lazy_section = "\n".join(lazy_lines)

        all_lines = [f"    '{name}'," for name in sorted(all_exports)]
        all_section = "\n".join(all_lines)

        new_content = f'''"""
Models 包 - 懒加载版本
所有模型类通过 __getattr__ 按需导入，避免启动时一次性加载所有模型文件。
Base 保持立即导入（SQLAlchemy 元数据初始化必需）。
由代码生成器自动生成 - 请勿手动修改
"""

from sqlalchemy.orm import declarative_base

Base = declarative_base()

# ==================== 懒加载映射表 ====================
_LAZY_IMPORTS = {{
{lazy_section}
}}

_loaded_models = {{}}


def __getattr__(name):
    """模块级 __getattr__：按需懒加载模型类"""
    if name in _loaded_models:
        return _loaded_models[name]

    module_path = _LAZY_IMPORTS.get(name)
    if module_path is not None:
        import importlib
        module = importlib.import_module(module_path, package='shared.models')
        cls = getattr(module, name)
        globals()[name] = cls
        _loaded_models[name] = cls
        return cls

    raise AttributeError(f"module 'shared.models' has no attribute {{name!r}}")


__all__ = [
{all_section}
]
'''

        self._write_file(init_path, new_content)
        print(f"  [OK] 更新 shared/models/__init__.py（懒加载模式，{len(lazy_entries)} 个模型）")

    def _render_template(self, template_name: str, context: dict) -> str:
        """渲染 Jinja2 模板"""
        try:
            template = self.jinja_env.get_template(template_name)
            return template.render(**context)
        except TemplateNotFound:
            print(f"  ⚠️ 模板未找到：{template_name}")
            return ""

    def _write_file(self, file_path: Path, content: str):
        """写入文件，确保编码为 UTF-8"""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"    已写入：{file_path}")

    def _format_generated_files(self):
        """使用 black 格式化生成的 Python 文件"""
        print("\n正在格式化生成的文件...")

        files_to_format = []

        # 添加 SQLAlchemy 模型文件
        if SHARED_MODELS_DIR.exists():
            files_to_format.extend(p for p in SHARED_MODELS_DIR.glob("*.py") if p.name != "__init__.py")

        files_to_format = [f for f in files_to_format if f is not None and f.exists()]

        if not files_to_format:
            print("  ⚠️ 没有需要格式化的文件")
            return

        try:
            subprocess.run(['black', '--version'], capture_output=True, text=True, timeout=5, check=True)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            print("  ⚠️ black 未安装，跳过格式化")
            return
        except subprocess.CalledProcessError:
            print("  ⚠️ black 无法运行，跳过格式化")
            return

        for file_path in files_to_format:
            print(f"  格式化：{file_path}")
            subprocess.run(['black', '-q', str(file_path)], capture_output=True, text=True, timeout=30)

        print("  ✅ 格式化完成")

    def sync_to_django(self, app_name: str = None):
        """将 SQLAlchemy 模型同步为 Django 模型（部分功能）"""
        print("🔄 开始同步 SQLAlchemy 模型到 Django...\n")
        print("步骤 1: 生成 Django Mixin...")
        # self._generate_orm_mixins()  # 该方法未实现，可忽略
        if app_name:
            print(f"\n步骤 2: 为 app '{app_name}' 生成 models.py...")
            print("  ⚠️ Django 模型生成功能尚未实现")
            print("  提示：请手动在 apps/{app_name}/models.py 中定义 Django 模型")
        else:
            print("\nℹ️ 未指定 app 名称，仅生成 Mixin")
        print("\n✅ 同步完成！")

    def _optimize_sqlalchemy_imports(self):
        """使用 isort 优化 SQLAlchemy 模型的导入"""
        print("\n正在优化 SQLAlchemy 模型的导入...")

        if not SHARED_MODELS_DIR.exists():
            print("  ⚠️ shared/models 目录不存在")
            return

        py_files = [p for p in SHARED_MODELS_DIR.glob("*.py") if p.name != "__init__.py"]
        if not py_files:
            print("  ⚠️ 没有需要优化导入的文件")
            return

        try:
            subprocess.run(['isort', '--version'], capture_output=True, text=True, timeout=5, check=True)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            print("  ⚠️ isort 未安装，跳过导入优化")
            return
        except subprocess.CalledProcessError:
            print("  ⚠️ isort 无法运行，跳过导入优化")
            return

        for file_path in py_files:
            print(f"  优化导入：{file_path}")
            subprocess.run(['isort', '-q', str(file_path)], capture_output=True, text=True, timeout=30)

        print("  ✅ 导入优化完成")


def main():
    parser = argparse.ArgumentParser(description='模型代码生成器')
    parser.add_argument('--config', type=str, default=None, help='routes.yaml 配置文件路径')
    parser.add_argument('command', nargs='?', choices=['generate-all', 'generate-model'],
                        default='generate-all', help='要执行的命令（默认：generate-all）')
    parser.add_argument('--model', type=str, nargs='+',
                        help='要生成的模型名称（用于 generate-model 命令），如 --model User TeamComment')

    args = parser.parse_args()

    try:
        generator = RouteGenerator(config_path=args.config)

        if args.command == 'generate-model':
            if not args.model:
                print("错误：请指定要生成的模型名称，如 --model User TeamComment")
                sys.exit(1)
            generator.generate_single_model(args.model)
        else:
            generator.generate_all()

    except FileNotFoundError as e:
        print(f"错误：{e}")
        sys.exit(1)
    except Exception as e:
        print(f"生成失败：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
