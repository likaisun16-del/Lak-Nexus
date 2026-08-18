"""错误类型定义：为配置、安全、工具、模型和任务状态提供可识别语义，供各层统一捕获。"""


class NexusError(Exception):
    """所有可预期的立凯中枢错误基类。"""


class ConfigError(NexusError):
    """配置缺失或配置值无效。"""


class ValidationError(NexusError):
    """工具参数不符合契约。"""


class PathAccessError(NexusError):
    """路径位于工作区外或无法安全解析。"""


class CommandDeniedError(NexusError):
    """命令未通过受控命令策略。"""


class ApprovalDeniedError(NexusError):
    """用户拒绝了高风险操作审批。"""


class ToolExecutionError(NexusError):
    """具体工具执行失败。"""


class AuditError(NexusError):
    """审计记录失败，必须阻止任务继续伪装成成功。"""


class ModelBackendError(NexusError):
    """模型后端调用或响应转换失败。"""


class TaskAlreadyExistsError(NexusError):
    """任务 ID 已存在，避免重复创建任务。"""


class TaskCancelledError(NexusError):
    """任务收到取消信号并停止后续执行。"""
