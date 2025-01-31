from maascommon.enums.node import NodeStatus

NODE_FAILURE_STATUS_TRANSITION_MAP = {
    NodeStatus.COMMISSIONING: NodeStatus.FAILED_COMMISSIONING,
    NodeStatus.DEPLOYING: NodeStatus.FAILED_DEPLOYMENT,
    NodeStatus.RELEASING: NodeStatus.FAILED_RELEASING,
    NodeStatus.DISK_ERASING: NodeStatus.FAILED_DISK_ERASING,
    NodeStatus.ENTERING_RESCUE_MODE: NodeStatus.FAILED_ENTERING_RESCUE_MODE,
    NodeStatus.EXITING_RESCUE_MODE: NodeStatus.FAILED_EXITING_RESCUE_MODE,
    NodeStatus.TESTING: NodeStatus.FAILED_TESTING,
}
