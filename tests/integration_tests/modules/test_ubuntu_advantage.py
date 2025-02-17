import json
import os

import pytest
from pycloudlib.cloud import ImageType

from tests.integration_tests.clouds import ImageSpecification, IntegrationCloud
from tests.integration_tests.conftest import get_validated_source
from tests.integration_tests.instances import (
    CloudInitSource,
    IntegrationInstance,
)
from tests.integration_tests.util import verify_clean_log

CLOUD_INIT_UA_TOKEN = os.environ.get("CLOUD_INIT_UA_TOKEN")

ATTACH_FALLBACK = """\
#cloud-config
ubuntu_advantage:
  features:
    disable_auto_attach: true
  token: {token}
"""

ATTACH = """\
#cloud-config
ubuntu_advantage:
  token: {token}
  enable:
  - esm-infra
"""

# bootcmd disables UA daemon on gce
UA_DAILY = """\
#cloud-config
apt:
  sources:
    ua-daily:
      source: 'ppa:ua-client/daily'
package_update: true
package_upgrade: true
packages:
- ubuntu-advantage-tools
bootcmd:
- sudo systemctl mask ubuntu-advantage.service
"""

AUTO_ATTACH_CUSTOM_SERVICES = """\
#cloud-config
ubuntu_advantage:
  enable:
  - livepatch
"""


def did_ua_service_noop(client: IntegrationInstance) -> bool:
    ua_log = client.read_from_file("/var/log/ubuntu-advantage.log")
    return (
        "Skipping auto-attach and deferring to cloud-init to setup and"
        " configure auto-attach" in ua_log
    )


def is_attached(client: IntegrationInstance) -> bool:
    status_resp = client.execute("sudo pro status --format json")
    assert status_resp.ok
    status = json.loads(status_resp.stdout)
    return bool(status.get("attached"))


def get_services_status(client: IntegrationInstance) -> dict:
    """Creates a map of service -> is_enable.

    pro status --format json contains a key with list of service objects like:

    {
      ...
      "services":[
        {
          "available":"yes",
          "blocked_by":[

          ],
          "description":"Common Criteria EAL2 Provisioning Packages",
          "description_override":null,
          "entitled":"yes",
          "name":"cc-eal",
          "status":"disabled",
          "status_details":"CC EAL2 is not configured"
        },
        ...
      ]
    }

    :return: Dict where the keys are ua service names and the values
    are booleans representing if the service is enable or not.
    """
    status_resp = client.execute("sudo pro status --format json")
    assert status_resp.ok
    status = json.loads(status_resp.stdout)
    return {
        svc["name"]: svc["status"] == "enabled" for svc in status["services"]
    }


@pytest.mark.adhoc
@pytest.mark.ubuntu
class TestUbuntuAdvantage:
    @pytest.mark.user_data(ATTACH_FALLBACK.format(token=CLOUD_INIT_UA_TOKEN))
    def test_valid_token(self, client: IntegrationInstance):
        assert CLOUD_INIT_UA_TOKEN, "CLOUD_INIT_UA_TOKEN env var not provided"
        log = client.read_from_file("/var/log/cloud-init.log")
        verify_clean_log(log)
        assert is_attached(client)

    @pytest.mark.user_data(ATTACH.format(token=CLOUD_INIT_UA_TOKEN))
    def test_idempotency(self, client: IntegrationInstance):
        assert CLOUD_INIT_UA_TOKEN, "CLOUD_INIT_UA_TOKEN env var not provided"
        log = client.read_from_file("/var/log/cloud-init.log")
        verify_clean_log(log)
        assert is_attached(client)

        # Clean reboot to change instance-id and trigger cc_ua in next boot
        assert client.execute("cloud-init clean --logs").ok
        client.restart()

        log = client.read_from_file("/var/log/cloud-init.log")
        verify_clean_log(log)
        assert is_attached(client)


def install_ua_daily(session_cloud: IntegrationCloud):
    """Install `ubuntu-advantage-tools` from ppa:ua-client/daily in an
    Ubuntu Pro image.

    TODO: Remove this after UA releases v28.0.
    """
    cfg_image_spec = ImageSpecification.from_os_image()
    with session_cloud.launch(
        user_data=UA_DAILY,
        launch_kwargs={
            "image_id": session_cloud.cloud_instance.daily_image(
                cfg_image_spec.image_id, image_type=ImageType.PRO
            )
        },
    ) as client:
        log = client.read_from_file("/var/log/cloud-init.log")
        verify_clean_log(log)
        client.execute("sudo pro detach --assume-yes")  # Force detach
        assert not is_attached(
            client
        ), "Test precondition error. Instance is auto-attached."
        source = get_validated_source(session_cloud)

        if source is CloudInitSource.NONE:
            # Confirm cloud-init now supports auto-attach customization
            client.write_to_file("/tmp/auto-attach.cfg", ATTACH_FALLBACK)
            result = client.execute(
                "cloud-init schema -c /tmp/auto-attach.cfg"
            )
            assert result.ok, (
                "cloud-init in image doesn't support custom auto-attach."
                " Try CLOUD_INIT_SOURCE=ppa:cloud-init-dev/daily."
            )
        else:
            client.install_new_cloud_init(source)

        client.destroy()


@pytest.mark.adhoc
@pytest.mark.azure
@pytest.mark.ec2
@pytest.mark.gce
@pytest.mark.ubuntu
class TestUbuntuAdvantagePro:
    def test_custom_services(self, session_cloud: IntegrationCloud):
        install_ua_daily(session_cloud)
        with session_cloud.launch(
            user_data=AUTO_ATTACH_CUSTOM_SERVICES,
            launch_kwargs={
                "image_id": session_cloud.snapshot_id,
            },
        ) as client:
            log = client.read_from_file("/var/log/cloud-init.log")
            verify_clean_log(log)
            assert did_ua_service_noop(client)
            assert is_attached(client)
            services_status = get_services_status(client)
            assert services_status.pop(
                "livepatch"
            ), "livepatch expected to be enabled"
            enabled_services = {
                svc for svc, status in services_status.items() if status
            }
            assert (
                not enabled_services
            ), f"Only livepatch must be enabled. Found: {enabled_services}"
