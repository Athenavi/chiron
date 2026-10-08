"""
Jinja2 自定义过滤器
用于路由代码生成器
"""

from datetime import datetime


def map_ts_type(python_type: str, param_name: str = '') -> str:
    """
    将 Python 类型映射为 TypeScript 类型

    Args:
        python_type: Python 类型名称
        param_name: 参数名称 (用于调试)

    Returns:
        TypeScript 类型字符串
    """
    type_mapping = {
        'int': 'number',
        'integer': 'number',
        'float': 'number',
        'double': 'number',
        'str': 'string',
        'string': 'string',
        'bool': 'boolean',
        'boolean': 'boolean',
        'datetime': 'string',  # ISO 8601 格式
        'date': 'string',
        'time': 'string',
        'uuid': 'string',
        'email': 'string',
        'any': 'any',
        'object': 'Record<string, any>',
        'array': 'any[]',
        'list': 'any[]',
        'dict': 'Record<string, any>',
    }

    # 处理可空类型
    if python_type is None:
        return 'any'

    # 转换为小写进行匹配
    type_lower = python_type.lower() if isinstance(python_type, str) else str(python_type).lower()

    return type_mapping.get(type_lower, 'any')


def map_ninja_type(python_type: str) -> str:
    """
    将 Python 类型映射为 Python 类型注解 (用于 Django Ninja)

    Args:
        python_type: Python 类型名称 (如 'int', 'str', 'float')

    Returns:
        Python 类型注解字符串
    """
    type_mapping = {
        'int': 'int',
        'integer': 'int',
        'float': 'float',
        'double': 'float',
        'str': 'str',
        'string': 'str',
        'bool': 'bool',
        'boolean': 'bool',
        'list': 'list',
        'dict': 'dict',
        'any': 'Any',
    }

    if python_type is None:
        return 'Any'

    type_lower = python_type.lower() if isinstance(python_type, str) else str(python_type).lower()
    return type_mapping.get(type_lower, 'Any')


def capitalize_first(s: str) -> str:
    """
    首字母大写

    Args:
        s: 输入字符串

    Returns:
        首字母大写的字符串
    """
    if not s:
        return ''
    return s[0].upper() + s[1:] if len(s) > 1 else s.upper()


def indent(text: str, width: int = 4) -> str:
    """
    为文本添加缩进

    Args:
        text: 输入文本
        width: 缩进空格数

    Returns:
        添加缩进后的文本
    """
    if not text:
        return ''
    indent_str = ' ' * width
    lines = text.split('\n')
    # 过滤掉空行并添加缩进
    indented_lines = [indent_str + line if line.strip() else '' for line in lines]
    return '\n'.join(indented_lines)


def camel_to_underscore(name: str) -> str:
    """
    将驼峰命名转换为下划线命名（复数形式）
    例如：SystemSettings -> system_settings
         User -> users
         VIPPlan -> vip_plans

    Args:
        name: 驼峰命名的类名

    Returns:
        下划线命名的表名（复数形式）
    """
    import re
    # 在大写字母前插入下划线
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    s2 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1)
    # 转为小写
    result = s2.lower()
    # 如果已经以 's' 结尾，不再添加 's'
    if not result.endswith('s'):
        result += 's'
    return result


def get_table_name(model_name: str, all_models: dict) -> str:
    """
    根据模型名称获取实际的表名（包含前缀）
    优先使用模型定义中的 table 配置，如果没有则使用 camel_to_underscore 推断

    Args:
        model_name: 模型名称（如 Media, MediaCategory）
        all_models: 所有模型的配置字典

    Returns:
        实际的表名（带前缀）
    """
    # 从环境变量或配置中获取表前缀
    try:
        from src.setting import settings
        table_prefix = getattr(settings, 'db_table_prefix', '')
    except Exception:  # noqa: BLE001 - 读不到配置就用空前缀
        table_prefix = ''

    # 查找模型配置
    model_def = all_models.get(model_name, {})
    if model_def and 'table' in model_def:
        base_table_name = model_def['table']
    else:
        # 如果没有配置，使用默认规则推断
        base_table_name = camel_to_underscore(model_name)

    # 添加表前缀
    return f"{table_prefix}{base_table_name}"


def now() -> str:
    """返回当前时间字符串"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def register_filters(env):
    """
    注册自定义过滤器到 Jinja2 环境

    Args:
        env: Jinja2 Environment 实例
    """
    env.filters['map_ts_type'] = map_ts_type
    env.filters['map_ninja_type'] = map_ninja_type
    env.filters['capitalize'] = capitalize_first
    env.filters['indent'] = indent  # 添加缩进过滤器
    env.filters['camel_to_underscore'] = camel_to_underscore
    env.filters['get_table_name'] = get_table_name  # 添加获取表名的过滤器
    env.globals['now'] = now
    env.globals['map_ts_type'] = map_ts_type
    env.globals['map_ninja_type'] = map_ninja_type
