# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from datetime import datetime
from typing import Any, Union

from pydantic import Field

from maascommon.enums.notifications import NotificationCategoryEnum
from maasservicelayer.models.base import ResourceBuilder, UNSET, Unset


class NotificationBuilder(ResourceBuilder):
    """Autogenerated from utilities/generate_builders.py.

    You can still add your custom methods here, they won't be overwritten by
    the generated code.
    """

    admins: Union[bool, Unset] = Field(default=UNSET, required=False)
    category: Union[NotificationCategoryEnum, Unset] = Field(
        default=UNSET, required=False
    )
    context: Union[dict[str, Any], Unset] = Field(
        default=UNSET, required=False
    )
    created: Union[datetime, Unset] = Field(default=UNSET, required=False)
    dismissable: Union[bool, Unset] = Field(default=UNSET, required=False)
    ident: Union[str, None, Unset] = Field(default=UNSET, required=False)
    message: Union[str, Unset] = Field(default=UNSET, required=False)
    updated: Union[datetime, Unset] = Field(default=UNSET, required=False)
    user_id: Union[int, None, Unset] = Field(default=UNSET, required=False)
    users: Union[bool, Unset] = Field(default=UNSET, required=False)


class NotificationDismissalBuilder(ResourceBuilder):
    """Autogenerated from utilities/generate_builders.py.

    You can still add your custom methods here, they won't be overwritten by
    the generated code.
    """

    created: Union[datetime, Unset] = Field(default=UNSET, required=False)
    notification_id: Union[int, Unset] = Field(default=UNSET, required=False)
    updated: Union[datetime, Unset] = Field(default=UNSET, required=False)
    user_id: Union[int, Unset] = Field(default=UNSET, required=False)
