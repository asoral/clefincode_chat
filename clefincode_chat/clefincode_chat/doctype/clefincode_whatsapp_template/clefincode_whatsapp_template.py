import frappe
from frappe.model.document import Document

from bs4 import BeautifulSoup
import requests
from requests.auth import HTTPBasicAuth
import json
from clefincode_chat.utils.utils import get_access_token, get_auth_token_twillio


def _sanitize_name(name: str) -> str:
    """make lowercase, replace invalid chars with underscores"""
    if not name:
        return ""
    safe = name.lower()
    safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in safe)
    return safe

class ClefinCodeWhatsAppTemplate(Document):
    def before_insert(self):
        if self.whatsapp_business_account_id:
            self.template_name = f"{self.whatsapp_business_account_id}_{self.meta_template_name}"
        else:
            self.template_name = f"{self.whatsapp_profile}_{self.meta_template_name}"
    
    def on_submit(self):
        try:
            if not self.whatsapp_profile:
                frappe.throw("Please set Whatsapp Profile before submit")

            profile = frappe.get_doc("ClefinCode WhatsApp Profile", self.whatsapp_profile)
            provider = (getattr(profile, "provider", "") or "").strip().lower()

            if provider in ("meta", "facebook", "waba"):
               
                if not getattr(profile, "business_account_id", None):
                    frappe.throw("WhatsApp Business Account ID not set on profile")
                self.post_whatsapp_template_meta()
            elif provider in ("twilio",):
                self.post_whatsapp_template_twilio()
            else:
                frappe.throw(f"Unknown whatsapp provider '{provider}' on profile {profile.name}")

        except Exception as e:
            frappe.log_error(message=f"on_submit error for template {self.name}: {str(e)}", title="ClefinCodeWhatsAppTemplate.on_submit")
            frappe.throw(str(e))
    
    def post_whatsapp_template_meta(self):
        try:
            import time
            access_token = get_access_token()
            api_base = "https://graph.facebook.com/v23.0"

            # First, check if template already exists
            existing_template = self._check_existing_template(access_token, api_base)

            if existing_template:
                template_id = existing_template.get("id")
                template_status = existing_template.get("status", "").upper()
                existing_category = existing_template.get("category", "").upper()

                # If template is approved, just link it to the current record
                if template_status == "APPROVED":
                    frappe.msgprint(f"Template '{self.meta_template_name}' already exists and is approved. Linking to existing template.", indicator="green")
                    self.whatsapp_template_id = template_id
                    self.template_status = template_status
                    # Update category to match existing approved template
                    if existing_category and existing_category != self.category.upper():
                        self.category = existing_category
                        frappe.msgprint(f"Updated category to '{existing_category}' to match existing approved template.", indicator="blue")
                    self.save()
                    frappe.db.commit()
                    return

                # If not approved, delete and recreate
                frappe.msgprint(f"Template '{self.meta_template_name}' exists but is not approved (status: {template_status}). Deleting and recreating.", indicator="orange")
                self._delete_template(template_id, access_token, api_base)
                # Wait for deletion to complete
                time.sleep(2)

            # Create new template
            endpoint = f"{api_base}/{self.whatsapp_business_account_id}/message_templates"

            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }

            data = {
                "name": self.meta_template_name,
                "category": self.category.upper() if self.category else "MARKETING",
                "language": self.template_language,
                "components": [
                    {
                        "type": "BODY",
                        "text": BeautifulSoup(self.body, 'html.parser').get_text()
                    },
                    {
                        "type": "BUTTONS",
                        "buttons": [
                            {
                                "type": "QUICK_REPLY",
                                "text": "Yes"
                            },
                            {
                                "type": "QUICK_REPLY",
                                "text": "No"
                            }
                        ]
                    }
                ]
            }

            # Try to create with retries
            max_retries = 3
            retry_delay = 2

            for attempt in range(max_retries):
                response = requests.post(endpoint, json=data, headers=headers)
                print("response:---->", response.text)

                if response.ok:
                    frappe.msgprint(f"WhatsApp message template <b><a href='/app/clefincode-whatsapp-template/{self.name}' target='_blank'>{self.meta_template_name}</a></b> has been created to proceed communication with the customer outside of the 24-hour window")
                    response_data = response.json()
                    self.whatsapp_template_id = response_data.get("id")
                    self.template_status = response_data.get("status")
                    self.save()
                    frappe.db.commit()
                    return

                # Parse error response
                try:
                    error_data = response.json()
                except:
                    error_data = {}

                error_subcode = error_data.get("error", {}).get("error_subcode")
                error_msg = error_data.get("error", {}).get("error_user_msg", "")

                # Error 2388025: Template being deleted or category mismatch
                if error_subcode == 2388025:
                    if "MARKETING" in error_msg and attempt < max_retries - 1:
                        # If it suggests using MARKETING, try with that category
                        if self.category.upper() != "MARKETING":
                            frappe.msgprint(f"Switching category to MARKETING as suggested by Meta API.", indicator="orange")
                            data["category"] = "MARKETING"
                            self.category = "MARKETING"
                            continue
                        else:
                            # Wait and retry
                            frappe.msgprint(f"Template deletion in progress. Waiting {retry_delay} seconds before retry {attempt + 1}/{max_retries}...", indicator="yellow")
                            time.sleep(retry_delay)
                            continue
                    elif attempt < max_retries - 1:
                        # Wait and retry
                        frappe.msgprint(f"Waiting {retry_delay} seconds before retry {attempt + 1}/{max_retries}...", indicator="yellow")
                        time.sleep(retry_delay)
                        continue

                # If last attempt or different error, throw
                if attempt == max_retries - 1:
                    frappe.throw(response.text)

        except frappe.ValidationError:
            # Re-raise validation errors from frappe.throw
            raise
        except Exception as e:
            frappe.log_error(f"post_whatsapp_template_meta exception: {str(e)}", "post_whatsapp_template_meta")
            frappe.throw(str(e))

    def _check_existing_template(self, access_token, api_base):
        """Check if template already exists in Meta API. Returns template dict or None."""
        try:
            get_endpoint = f"{api_base}/{self.whatsapp_business_account_id}/message_templates"
            headers = {"Authorization": f"Bearer {access_token}"}
            params = {"name": self.meta_template_name}

            response = requests.get(get_endpoint, headers=headers, params=params)
            if response.ok:
                data = response.json()
                templates = data.get("data", [])
                if templates:
                    return templates[0]
            return None
        except Exception as e:
            frappe.log_error(f"Failed to check existing template: {str(e)}", "check_template_error")
            return None

    def _delete_template(self, template_id, access_token, api_base):
        """Delete a template from Meta API"""
        try:
            delete_endpoint = f"{api_base}/{self.whatsapp_business_account_id}/message_templates"
            headers = {"Authorization": f"Bearer {access_token}"}
            params = {"name": self.meta_template_name}

            response = requests.delete(delete_endpoint, headers=headers, params=params)
            if response.ok:
                frappe.msgprint(f"Successfully deleted template '{self.meta_template_name}' from Meta.", indicator="blue")
            else:
                frappe.log_error(f"Failed to delete template: {response.text}", "delete_template_error")
                frappe.msgprint(f"Warning: Could not delete existing template. Proceeding with creation.", indicator="yellow")
        except Exception as e:
            frappe.log_error(f"Exception deleting template: {str(e)}", "delete_template_exception")
            frappe.msgprint(f"Warning: Could not delete existing template. Proceeding with creation.", indicator="yellow")
    
    def post_whatsapp_template_twilio(self):
        """
        Create Content template in Twilio and submit approval for WhatsApp.
        Updates: whatsapp_template_id (content SID), template_status
        """
        try:
            # read Twilio creds from Integration doctype
            doc = frappe.get_doc("ClefinCode Twilio Integration")
            account_sid = doc.get("account_sid")
            auth_token = get_auth_token_twillio()
            if not account_sid or not auth_token:
                frappe.throw("Twilio credentials not configured in ClefinCode WhatsApp Integration")

            # prepare content payload
            # use safe name for twilio approval (lowercase underscores)
            safe_name = _sanitize_name(self.meta_template_name or f"template_{self.name}")
            payload = {
                "friendly_name": safe_name,
                "language": self.template_language or "en",
                "variables": {"1": (self.meta_template_name or "")},
                "types": {
                    "twilio/text": {"body": BeautifulSoup(self.body or "", "html.parser").get_text()}
                }
            }

            # add quick-reply buttons if exist
            if getattr(self, "buttons", None) and len(self.buttons) > 0:
                qr_actions = []
                for idx, b in enumerate(self.buttons):
                            
                           
                    btn_title = b.get("button_text")
                    qr_actions.append({"title": btn_title, "id": btn_title})
                payload["types"]["twilio/quick-reply"] = {"body": BeautifulSoup(self.body or "", "html.parser").get_text(), "actions": qr_actions}

            # 1) create content
            create_url = "https://content.twilio.com/v1/Content"
            create_resp = requests.post(create_url, json=payload, auth=HTTPBasicAuth(account_sid, auth_token), timeout=30)

            try:
                create_data = create_resp.json()
            except ValueError:
                create_data = {"raw": create_resp.text}

            if create_resp.status_code >= 400:
                frappe.log_error(create_resp.text, "twilio_create_content_error")
                frappe.throw(f"Twilio create content failed: {create_resp.status_code} - {create_resp.text}")

            content_sid = create_data.get("sid")
            if not content_sid:
                frappe.throw(f"No content SID returned from Twilio: {create_data}")

            # save content sid as whatsapp_template_id
            self.whatsapp_template_id = content_sid
            self.template_status = "PENDING"
            self.save()
            frappe.db.commit()

            # 2) submit approval for whatsapp (name must be lowercase+underscores)
            approve_url = f"https://content.twilio.com/v1/Content/{content_sid}/ApprovalRequests/whatsapp"
            approve_payload = {"name": safe_name, "category": (self.category or "UTILITY").upper()}

            approve_resp = requests.post(approve_url, json=approve_payload, auth=HTTPBasicAuth(account_sid, auth_token), timeout=30)
            try:
                approve_data = approve_resp.json()
            except ValueError:
                approve_data = {"raw": approve_resp.text}

            if approve_resp.status_code >= 400:
                frappe.log_error(approve_resp.text, "twilio_approval_error")
                # Twilio may reject because WABA not linked — show message and set status REJECTED
                self.template_status = "REJECTED"
                self.save()
                frappe.db.commit()
                frappe.throw(f"Twilio approval failed: {approve_resp.status_code} - {approve_resp.text}")
            else:
                url = f"https://content.twilio.com/v1/Content/{content_sid}/ApprovalRequests"
                resp = requests.get(url, auth=HTTPBasicAuth(account_sid, auth_token), timeout=20)

                try:
                    resp_json = resp.json()
                except ValueError:
                    resp_json = {"raw": resp.text}

                if resp.status_code >= 400:
                    return {"status": "error", "http_status": resp.status_code, "response": resp_json}
                
                # rejection_reason = (resp_json.get("rejection_reason") or "") if isinstance(approval, dict) else ""
       
                whatsapp_res = resp_json.get("whatsapp")
                twilio_status = (whatsapp_res.get("status")).strip().lower()
                
                _status_map = {
                    "received": "PENDING",
                    "pending": "PENDING",
                    "approved": "APPROVED",
                    "rejected": "REJECTED",
                    "failed": "REJECTED"
                }
                mapped_status = _status_map.get(twilio_status, "PENDING")
    
                # success: store approval response (status may be 'received' or 'pending')
                # Twilio returns fields like 'status' and 'category'
                self.template_status = mapped_status
                self.save()
                frappe.db.commit()
                frappe.msgprint(f"Template created in Twilio (sid: {content_sid}) and submitted for WhatsApp approval (status: {self.template_status})")

        except Exception as e:
            frappe.log_error(f"post_whatsapp_template_twilio exception: {str(e)}", "post_whatsapp_template_twilio")
            frappe.throw(str(e))