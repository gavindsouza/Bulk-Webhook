# Copyright (c) 2021, Aakvatech and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import calendar
from datetime import timedelta
from six.moves.urllib.parse import urlparse
import requests
import base64
import hashlib
import hmac
import json
from time import sleep
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import (
    now_datetime,
    today,
    add_to_date,
)
from frappe.utils.jinja import validate_template
from frappe.utils.safe_exec import get_safe_globals

from console import console

WEBHOOK_SECRET_HEADER = "X-Frappe-Webhook-Signature"


class BulkWebhook(Document):
    def validate(self):
        self.validate_mandatory_fields()
        self.validate_request_url()
        self.validate_request_body()

    def validate_request_url(self):
        try:
            request_url = urlparse(self.request_url).netloc
            if not request_url:
                raise frappe.ValidationError
        except Exception as e:
            frappe.throw(_("Check Request URL"), exc=e)

    def validate_request_body(self):
        if self.request_structure:
            if self.request_structure == "Form URL-Encoded":
                self.webhook_json = None
            elif self.request_structure == "JSON":
                validate_template(self.webhook_json)
                self.webhook_data = []

    def validate_mandatory_fields(self):
        # Check if all Mandatory Report Filters are filled by the User
        filters = frappe.parse_json(self.filters) if self.filters else {}
        filter_meta = frappe.parse_json(self.filter_meta) if self.filter_meta else {}
        throw_list = []
        for meta in filter_meta:
            if meta.get("reqd") and not filters.get(meta["fieldname"]):
                throw_list.append(meta["label"])
        if throw_list:
            frappe.throw(
                title=_("Missing Filters Required"),
                msg=_("Following Report Filters have missing values:")
                + "<br><br><ul><li>"
                + " <li>".join(throw_list)
                + "</ul>",
            )

    def get_report_data(self):
        """Returns file in for the report in given format"""
        report = frappe.get_doc("Report", self.report)

        self.filters = frappe.parse_json(self.filters) if self.filters else {}

        if self.report_type == "Report Builder" and self.data_modified_till:
            self.filters["modified"] = (
                ">",
                now_datetime() - timedelta(hours=self.data_modified_till),
            )

        if self.report_type != "Report Builder" and self.dynamic_date_filters_set():
            self.prepare_dynamic_filters()

        columns, data = report.get_data(
            user=self.user,
            filters=self.filters,
            as_dict=True,
            ignore_prepared_report=True,
        )

        # add serial numbers
        columns.insert(0, frappe._dict(fieldname="idx", label="", width="30px"))
        for i in range(len(data)):
            data[i]["idx"] = i + 1

        if len(data) == 0 and self.send_if_data:
            return None

        return data

    def prepare_dynamic_filters(self):
        self.filters = frappe.parse_json(self.filters)

        to_date = today()
        from_date_value = {
            "Daily": ("days", -1),
            "Weekly": ("weeks", -1),
            "Monthly": ("months", -1),
            "Quarterly": ("months", -3),
            "Half Yearly": ("months", -6),
            "Yearly": ("years", -1),
        }[self.dynamic_date_period]

        from_date = add_to_date(to_date, **{from_date_value[0]: from_date_value[1]})

        self.filters[self.from_date_field] = from_date
        self.filters[self.to_date_field] = to_date

    def send(self):
        if self.filter_meta and not self.filters:
            frappe.throw(_("Please set filters value in Report Filter table."))

        data = self.get_report_data()

        if not data:
            return

        enqueue_webhook(self)

    def dynamic_date_filters_set(self):
        return self.dynamic_date_period and self.from_date_field and self.to_date_field


@frappe.whitelist()
def send_now(name):
    """Send Auto Email report now"""
    webhook = frappe.get_doc("Bulk Webhook", name)
    webhook.check_permission()
    webhook.send()


def send_daily():
    """Check reports to be sent daily"""

    current_day = calendar.day_name[now_datetime().weekday()]
    enabled_reports = frappe.get_all(
        "Bulk Webhook",
        filters={"enabled": 1, "frequency": ("in", ("Daily", "Weekdays", "Weekly"))},
    )

    for report in enabled_reports:
        auto_email_report = frappe.get_doc("Bulk Webhook", report.name)

        # if not correct weekday, skip
        if auto_email_report.frequency == "Weekdays":
            if current_day in ("Saturday", "Sunday"):
                continue
        elif auto_email_report.frequency == "Weekly":
            if auto_email_report.day_of_week != current_day:
                continue
        try:
            auto_email_report.send()
        except Exception as e:
            frappe.log_error(
                e,
                _("Failed to send {0} Bulk Webhook").format(auto_email_report.name),
            )


def send_monthly():
    """Check reports to be sent monthly"""
    for report in frappe.get_all(
        "Bulk Webhook", {"enabled": 1, "frequency": "Monthly"}
    ):
        frappe.get_doc("Bulk Webhook", report.name).send()


def update_field_types(columns):
    for col in columns:
        if (
            col.fieldtype in ("Link", "Dynamic Link", "Currency")
            and col.options != "Currency"
        ):
            col.fieldtype = "Data"
            col.options = ""
    return columns


# Webhook
def get_context(data):
    return {"data": data, "utils": get_safe_globals().get("frappe").get("utils")}


def enqueue_webhook(webhook):
    webhook = frappe.get_doc("Bulk Webhook", webhook.get("name"))
    headers = get_webhook_headers(webhook)
    data = get_webhook_data(webhook)

    for i in range(3):
        try:
            r = requests.request(
                method=webhook.request_method,
                url=webhook.request_url,
                data=json.dumps(data, default=str),
                headers=headers,
                timeout=5,
            )
            r.raise_for_status()
            frappe.logger().debug({"webhook_success": r.text})
            log_request(webhook.request_url, headers, data, r)
            break
        except Exception as e:
            frappe.logger().debug({"webhook_error": e, "try": i + 1})
            log_request(webhook.request_url, headers, data, r)
            sleep(3 * i + 1)
            if i != 2:
                continue
            else:
                raise e


def log_request(url, headers, data, res):
    request_log = frappe.get_doc(
        {
            "doctype": "Webhook Request Log",
            "user": frappe.session.user if frappe.session.user else None,
            "url": url,
            "headers": json.dumps(headers, indent=4) if headers else None,
            "data": json.dumps(data, indent=4) if isinstance(data, dict) else data,
            "response": json.dumps(res.json(), indent=4) if res else None,
        }
    )

    request_log.insert(ignore_permissions=True)
    console(request_log).info()
    frappe.db.commit()


def get_webhook_headers(webhook):
    headers = {}

    if webhook.enable_security:
        data = get_webhook_data(webhook)
        signature = base64.b64encode(
            hmac.new(
                webhook.get_password("webhook_secret").encode("utf8"),
                json.dumps(data).encode("utf8"),
                hashlib.sha256,
            ).digest()
        )
        headers[WEBHOOK_SECRET_HEADER] = signature

    if webhook.webhook_headers:
        for h in webhook.webhook_headers:
            if h.get("key") and h.get("value"):
                headers[h.get("key")] = h.get("value")

    return headers


def get_webhook_data(webhook):
    data = {}
    _data = webhook.get_report_data()
    console(_data).info()
    if not _data:
        return

    if webhook.webhook_json:
        data = frappe.render_template(webhook.webhook_json, get_context(_data))
        console(data).info()

        data = json.loads(data)

    return data
