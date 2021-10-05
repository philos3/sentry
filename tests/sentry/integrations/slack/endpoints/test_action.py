from urllib.parse import parse_qs

import responses
from freezegun import freeze_time

from sentry.api import client
from sentry.integrations.slack.endpoints.action import (
    LINK_IDENTITY_MESSAGE,
    UNLINK_IDENTITY_MESSAGE,
)
from sentry.integrations.slack.views.link_identity import build_linking_url
from sentry.integrations.slack.views.unlink_identity import build_unlinking_url
from sentry.models import (
    AuthIdentity,
    AuthProvider,
    Group,
    GroupAssignee,
    GroupStatus,
    Identity,
    IdentityProvider,
    IdentityStatus,
    Integration,
    OrganizationIntegration,
)
from sentry.testutils import APITestCase
from sentry.utils import json
from sentry.utils.compat.mock import patch
from tests.sentry.integrations.slack import add_identity, install_slack


class BaseEventTest(APITestCase):
    def setUp(self):
        super().setUp()
        self.external_id = "slack:1"
        self.integration = install_slack(self.organization)
        self.idp = add_identity(self.integration, self.user, self.external_id)

        self.trigger_id = "13345224609.738474920.8088930838d88f008e0"
        self.response_url = (
            "https://hooks.slack.com/actions/T47563693/6204672533/x7ZLaiVMoECAW50Gw1ZYAXEM"
        )

    @patch(
        "sentry.integrations.slack.requests.SlackRequest._check_signing_secret", return_value=True
    )
    def post_webhook(
        self,
        check_signing_secret_mock,
        action_data=None,
        type="event_callback",
        data=None,
        team_id="TXXXXXXX1",
        callback_id=None,
        slack_user=None,
        original_message=None,
    ):

        if slack_user is None:
            slack_user = {"id": self.external_id, "domain": "example"}

        if callback_id is None:
            callback_id = json.dumps({"issue": self.group.id})

        if original_message is None:
            original_message = {}

        payload = {
            "team": {"id": team_id, "domain": "example.com"},
            "channel": {"id": "C065W1189", "domain": "forgotten-works"},
            "user": slack_user,
            "callback_id": callback_id,
            "action_ts": "1458170917.164398",
            "message_ts": "1458170866.000004",
            "original_message": original_message,
            "trigger_id": self.trigger_id,
            "response_url": self.response_url,
            "attachment_id": "1",
            "actions": action_data or [],
            "type": type,
        }
        if data:
            payload.update(data)

        payload = {"payload": json.dumps(payload)}

        return self.client.post("/extensions/slack/action/", data=payload)


class StatusActionTest(BaseEventTest):
    @freeze_time("2021-01-14T12:27:28.303Z")
    def test_ask_linking(self):
        """Freezing time to prevent flakiness from timestamp mismatch."""

        resp = self.post_webhook(slack_user={"id": "invalid-id", "domain": "example"})

        associate_url = build_linking_url(
            self.integration, self.organization, "invalid-id", "C065W1189", self.response_url
        )

        assert resp.status_code == 200, resp.content
        assert resp.data["response_type"] == "ephemeral"
        assert resp.data["text"] == LINK_IDENTITY_MESSAGE.format(associate_url=associate_url)

    def test_ignore_issue(self):
        status_action = {"name": "status", "value": "ignored", "type": "button"}

        resp = self.post_webhook(action_data=[status_action])
        self.group = Group.objects.get(id=self.group.id)

        assert resp.status_code == 200, resp.content
        assert self.group.get_status() == GroupStatus.IGNORED

        expect_status = f"*Issue ignored by <@{self.external_id}>*"
        assert resp.data["text"].endswith(expect_status), resp.data["text"]

    def test_ignore_issue_with_additional_user_auth(self):
        """
        Ensure that we can act as a user even when the organization has SSO enabled
        """
        auth_idp = AuthProvider.objects.create(organization=self.organization, provider="dummy")
        AuthIdentity.objects.create(auth_provider=auth_idp, user=self.user)

        status_action = {"name": "status", "value": "ignored", "type": "button"}

        resp = self.post_webhook(action_data=[status_action])
        self.group = Group.objects.get(id=self.group.id)

        assert resp.status_code == 200, resp.content
        assert self.group.get_status() == GroupStatus.IGNORED

        expect_status = f"*Issue ignored by <@{self.external_id}>*"
        assert resp.data["text"].endswith(expect_status), resp.data["text"]

    def test_assign_issue(self):
        user2 = self.create_user(is_superuser=False)
        self.create_member(user=user2, organization=self.organization, teams=[self.team])

        # Assign to user
        status_action = {
            "name": "assign",
            "selected_options": [{"value": f"user:{user2.id}"}],
        }

        resp = self.post_webhook(action_data=[status_action])

        assert resp.status_code == 200, resp.content
        assert GroupAssignee.objects.filter(group=self.group, user=user2).exists()

        expect_status = f"*Issue assigned to {user2.get_display_name()} by <@{self.external_id}>*"

        # Assign to team
        status_action = {
            "name": "assign",
            "selected_options": [{"value": f"team:{self.team.id}"}],
        }

        resp = self.post_webhook(action_data=[status_action])

        assert resp.status_code == 200, resp.content
        assert GroupAssignee.objects.filter(group=self.group, team=self.team).exists()

        expect_status = f"*Issue assigned to #{self.team.slug} by <@{self.external_id}>*"

        assert resp.data["text"].endswith(expect_status), resp.data["text"]

    def test_assign_issue_user_has_identity(self):
        user2 = self.create_user(is_superuser=False)
        self.create_member(user=user2, organization=self.organization, teams=[self.team])

        user2_identity = Identity.objects.create(
            external_id="slack_id2",
            idp=self.idp,
            user=user2,
            status=IdentityStatus.VALID,
            scopes=[],
        )

        status_action = {
            "name": "assign",
            "selected_options": [{"value": f"user:{user2.id}"}],
        }

        resp = self.post_webhook(action_data=[status_action])

        assert resp.status_code == 200, resp.content
        assert GroupAssignee.objects.filter(group=self.group, user=user2).exists()

        expect_status = (
            f"*Issue assigned to <@{user2_identity.external_id}> by <@{self.external_id}>*"
        )

        assert resp.data["text"].endswith(expect_status), resp.data["text"]

    def test_response_differs_on_bot_message(self):
        status_action = {"name": "status", "value": "ignored", "type": "button"}

        original_message = {"type": "message"}

        resp = self.post_webhook(action_data=[status_action], original_message=original_message)
        self.group = Group.objects.get(id=self.group.id)

        assert resp.status_code == 200, resp.content
        assert "attachments" in resp.data
        assert resp.data["attachments"][0]["title"] == self.group.title

    def test_assign_user_with_multiple_identities(self):
        org2 = self.create_organization(owner=None)

        integration2 = Integration.objects.create(
            provider="slack",
            external_id="TXXXXXXX2",
            metadata={"access_token": "xoxa-xxxxxxxxx-xxxxxxxxxx-xxxxxxxxxxxx"},
        )
        OrganizationIntegration.objects.create(organization=org2, integration=integration2)

        idp2 = IdentityProvider.objects.create(type="slack", external_id="TXXXXXXX2", config={})
        Identity.objects.create(
            external_id="slack_id2",
            idp=idp2,
            user=self.user,
            status=IdentityStatus.VALID,
            scopes=[],
        )

        status_action = {
            "name": "assign",
            "selected_options": [{"value": f"user:{self.user.id}"}],
        }

        resp = self.post_webhook(action_data=[status_action])

        assert resp.status_code == 200, resp.content
        assert GroupAssignee.objects.filter(group=self.group, user=self.user).exists()

        expect_status = "*Issue assigned to <@{assignee}> by <@{assignee}>*".format(
            assignee=self.external_id
        )

        assert resp.data["text"].endswith(expect_status), resp.data["text"]

    @responses.activate
    def test_resolve_issue(self):
        status_action = {"name": "resolve_dialog", "value": "resolve_dialog"}

        # Expect request to open dialog on slack
        responses.add(
            method=responses.POST,
            url="https://slack.com/api/dialog.open",
            body='{"ok": true}',
            status=200,
            content_type="application/json",
        )

        resp = self.post_webhook(action_data=[status_action])
        assert resp.status_code == 200, resp.content

        # Opening dialog should *not* cause the current message to be updated
        assert resp.content == b""

        data = parse_qs(responses.calls[0].request.body)
        assert data["token"][0] == self.integration.metadata["access_token"]
        assert data["trigger_id"][0] == self.trigger_id
        assert "dialog" in data

        dialog = json.loads(data["dialog"][0])
        callback_data = json.loads(dialog["callback_id"])
        assert int(callback_data["issue"]) == self.group.id
        assert callback_data["orig_response_url"] == self.response_url

        # Completing the dialog will update the message
        responses.add(
            method=responses.POST,
            url=self.response_url,
            body='{"ok": true}',
            status=200,
            content_type="application/json",
        )

        resp = self.post_webhook(
            type="dialog_submission",
            callback_id=dialog["callback_id"],
            data={"submission": {"resolve_type": "resolved"}},
        )
        self.group = Group.objects.get(id=self.group.id)

        assert resp.status_code == 200, resp.content
        assert self.group.get_status() == GroupStatus.RESOLVED

        update_data = json.loads(responses.calls[1].request.body)

        expect_status = f"*Issue resolved by <@{self.external_id}>*"
        assert update_data["text"].endswith(expect_status)

    def test_permission_denied(self):
        user2 = self.create_user(is_superuser=False)

        user2_identity = Identity.objects.create(
            external_id="slack_id2",
            idp=self.idp,
            user=user2,
            status=IdentityStatus.VALID,
            scopes=[],
        )

        status_action = {"name": "status", "value": "ignored", "type": "button"}

        resp = self.post_webhook(
            action_data=[status_action], slack_user={"id": user2_identity.external_id}
        )
        self.group = Group.objects.get(id=self.group.id)

        associate_url = build_unlinking_url(
            self.integration.id, "slack_id2", "C065W1189", self.response_url
        )

        assert resp.status_code == 200, resp.content
        assert resp.data["response_type"] == "ephemeral"
        assert not resp.data["replace_original"]
        assert resp.data["text"] == UNLINK_IDENTITY_MESSAGE.format(
            associate_url=associate_url, user_email=user2.email, org_name=self.organization.name
        )

    @freeze_time("2021-01-14T12:27:28.303Z")
    @responses.activate
    @patch("sentry.api.client.put")
    def test_handle_submission_fail(self, client_put):
        status_action = {"name": "resolve_dialog", "value": "resolve_dialog"}

        # Expect request to open dialog on slack
        responses.add(
            method=responses.POST,
            url="https://slack.com/api/dialog.open",
            body='{"ok": true}',
            status=200,
            content_type="application/json",
        )

        resp = self.post_webhook(action_data=[status_action])
        assert resp.status_code == 200, resp.content

        # Opening dialog should *not* cause the current message to be updated
        assert resp.content == b""

        data = parse_qs(responses.calls[0].request.body)
        assert data["token"][0] == self.integration.metadata["access_token"]
        assert data["trigger_id"][0] == self.trigger_id
        assert "dialog" in data

        dialog = json.loads(data["dialog"][0])
        callback_data = json.loads(dialog["callback_id"])
        assert int(callback_data["issue"]) == self.group.id
        assert callback_data["orig_response_url"] == self.response_url

        # Completing the dialog will update the message
        responses.add(
            method=responses.POST,
            url=self.response_url,
            body='{"ok": true}',
            status=200,
            content_type="application/json",
        )

        # make the client raise an API error
        client_put.side_effect = client.ApiError(
            403, '{"detail":"You do not have permission to perform this action."}'
        )

        resp = self.post_webhook(
            type="dialog_submission",
            callback_id=dialog["callback_id"],
            data={"submission": {"resolve_type": "resolved"}},
        )

        # TODO(mgaeta): `assert_called` is deprecated. Find a replacement.
        # client_put.assert_called()

        associate_url = build_unlinking_url(
            self.integration.id, self.external_id, "C065W1189", self.response_url
        )

        assert resp.status_code == 200, resp.content
        assert resp.data["text"] == UNLINK_IDENTITY_MESSAGE.format(
            associate_url=associate_url, user_email=self.user.email, org_name=self.organization.name
        )

    @patch(
        "sentry.integrations.slack.requests.SlackRequest._check_signing_secret", return_value=True
    )
    def test_no_integration(self, check_signing_secret_mock):
        self.integration.delete()
        resp = self.post_webhook()
        assert resp.status_code == 403

    @patch(
        "sentry.integrations.slack.requests.SlackRequest._check_signing_secret", return_value=True
    )
    def test_slack_bad_payload(self, check_signing_secret_mock):
        resp = self.client.post("/extensions/slack/action/", data={"nopayload": 0})
        assert resp.status_code == 400

    @patch(
        "sentry.integrations.slack.requests.SlackRequest._check_signing_secret", return_value=True
    )
    def test_sentry_docs_link_clicked(self, check_signing_secret_mock):
        payload = {
            "team": {"id": "TXXXXXXX1", "domain": "example.com"},
            "user": {"id": self.external_id, "domain": "example"},
            "type": "block_actions",
            "actions": [{"value": "sentry_docs_link_clicked"}],
        }

        payload = {"payload": json.dumps(payload)}

        resp = self.client.post("/extensions/slack/action/", data=payload)
        assert resp.status_code == 200