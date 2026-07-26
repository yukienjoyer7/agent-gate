"""Tests for the natural language → ActionRequest parser."""

from app.llm.services.parser import parse_prompt, parse_prompt_plan


class TestParsePrompt:
    """Verify the browser parser extracts correct fields from prompts."""

    def test_click_login_button(self) -> None:
        result = parse_prompt("Click the login button on playwright.dev")
        parsed = result["parsed"]

        assert parsed["action_type"] == "BROWSER_CLICK"
        assert parsed["target"] == "https://playwright.dev"
        assert parsed["payload"]["url"] == "https://playwright.dev"
        assert parsed["payload"]["label"] == "Login"
        assert parsed["payload"]["role"] == "button"
        assert parsed["browser_element"]["role"] == "button"
        assert parsed["browser_element"]["label"] == "Login"
        assert result["llm_provider"] == "dummy"
        assert result["raw_prompt"] == "Click the login button on playwright.dev"

    def test_type_search_query(self) -> None:
        result = parse_prompt("Type 'hello world' into the search box on google.com")
        parsed = result["parsed"]

        assert parsed["action_type"] == "BROWSER_TYPE"
        assert parsed["target"] == "https://google.com"
        assert parsed["payload"]["value"] == "hello world"

    def test_open_url(self) -> None:
        result = parse_prompt("Open youtube.com")
        parsed = result["parsed"]

        assert parsed["action_type"] == "BROWSER_OPEN"
        assert parsed["target"] == "https://youtube.com"

    def test_open_explicit_url(self) -> None:
        result = parse_prompt("go to https://example.com/login")
        parsed = result["parsed"]

        assert parsed["action_type"] == "BROWSER_OPEN"
        assert parsed["target"] == "https://example.com/login"

    def test_screenshot(self) -> None:
        result = parse_prompt("Take a screenshot of the page")
        parsed = result["parsed"]

        assert parsed["action_type"] == "BROWSER_SCREENSHOT"
        assert parsed["domain"] == "browser"
        assert parsed["source"] == "chat"

    def test_scroll_page(self) -> None:
        result = parse_prompt("Scroll down on news.example.com")
        parsed = result["parsed"]

        assert parsed["action_type"] == "BROWSER_SCROLL"
        assert parsed["target"] == "https://news.example.com"

    def test_submit_form(self) -> None:
        result = parse_prompt("Submit the contact form on my.site.com")
        parsed = result["parsed"]

        assert parsed["action_type"] == "BROWSER_SUBMIT"
        assert parsed["target"] == "https://my.site.com"
        assert parsed["risk_hint"] == "external_send"

    def test_no_url_uses_target_as_url(self) -> None:
        result = parse_prompt("Click the confirm button")
        parsed = result["parsed"]

        assert parsed["action_type"] == "BROWSER_CLICK"
        assert parsed["target"] == ""
        assert "label" in parsed["payload"]

    def test_fields_always_present(self) -> None:
        result = parse_prompt("Open a page")
        parsed = result["parsed"]

        assert parsed["source"] == "chat"
        assert parsed["domain"] == "browser"
        assert parsed["action_type"] is not None
        assert parsed["target_system"] == "browser"
        assert isinstance(parsed["payload"], dict)
        assert result["raw_prompt"] is not None
        assert result["llm_provider"] == "dummy"

    def test_human_readable_present(self) -> None:
        result = parse_prompt("Click the login button on playwright.dev")
        assert "human_readable" in result
        assert "Click" in result["human_readable"]


class TestConnectorParsing:
    """Verify connector domain detection and parsing."""

    def test_gmail_send_email(self) -> None:
        result = parse_prompt("Send email to john@example.com saying hello")
        parsed = result["parsed"]

        assert parsed["target_system"] == "gmail"
        assert parsed["domain"] == "productivity"
        assert parsed["action_type"] == "API_CALL"
        assert parsed["payload"]["action"] == "send"
        assert parsed["payload"]["to"] == "john@example.com"

    def test_gmail_read_email(self) -> None:
        result = parse_prompt("Read my latest email from inbox")
        parsed = result["parsed"]

        assert parsed["target_system"] == "gmail"
        assert parsed["payload"]["action"] == "read"

    def test_gmail_archive_email(self) -> None:
        result = parse_prompt("Archive email about meeting schedule")
        parsed = result["parsed"]

        assert parsed["target_system"] == "gmail"
        assert parsed["payload"]["action"] == "archive"
        assert "meeting" in parsed["payload"]["query"]

    def test_gmail_risk_hint_send(self) -> None:
        result = parse_prompt("Send email to user@test.com saying hello")
        parsed = result["parsed"]

        assert parsed["risk_hint"] == "external_send"

    def test_github_repo_info(self) -> None:
        result = parse_prompt("Get repo info for microsoft/vscode")
        parsed = result["parsed"]

        assert parsed["target_system"] == "github"
        assert parsed["domain"] == "code_protection"
        assert parsed["payload"]["action"] == "repo_metadata"
        assert parsed["payload"]["owner"] == "microsoft"
        assert parsed["payload"]["repo"] == "vscode"
        assert parsed["target"] == "microsoft/vscode"

    def test_github_repo_info_of(self) -> None:
        result = parse_prompt("Get repo info of facebook/react")
        parsed = result["parsed"]

        assert parsed["target_system"] == "github"
        assert parsed["payload"]["owner"] == "facebook"
        assert parsed["payload"]["repo"] == "react"

    def test_local_file_read(self) -> None:
        result = parse_prompt("Read file sample.txt")
        parsed = result["parsed"]

        assert parsed["target_system"] == "local_file"
        assert parsed["domain"] == "filesystem"
        assert parsed["action_type"] == "FILE_READ"
        assert parsed["payload"]["action"] == "read"
        assert parsed["payload"]["path"] == "sample.txt"
        assert parsed["risk_hint"] == "file_read"

    def test_local_file_read_with_path(self) -> None:
        result = parse_prompt("Read file data/config.json")
        parsed = result["parsed"]

        assert parsed["target_system"] == "local_file"
        assert parsed["payload"]["path"] == "data/config.json"


class TestConnectorPlanGeneration:
    """Verify plan generation for connector actions."""

    def test_gmail_plan_is_single_step(self) -> None:
        """Connector actions should never get BROWSER_OPEN prepended."""
        result = parse_prompt_plan("Send email to user@test.com saying hello")
        plan = result["plan"]

        assert len(plan) == 1
        assert plan[0]["target_system"] == "gmail"

    def test_github_plan_is_single_step(self) -> None:
        result = parse_prompt_plan("Get repo info for owner/repo")
        plan = result["plan"]

        assert len(plan) == 1
        assert plan[0]["target_system"] == "github"

    def test_file_plan_is_single_step(self) -> None:
        result = parse_prompt_plan("Read file notes.txt")
        plan = result["plan"]

        assert len(plan) == 1
        assert plan[0]["target_system"] == "local_file"

    def test_browser_plan_still_two_steps(self) -> None:
        """Browser actions with URL should still get BROWSER_OPEN."""
        result = parse_prompt_plan("Click the login button on playwright.dev")
        assert len(result["plan"]) == 2
        assert result["plan"][0]["action_type"] == "BROWSER_OPEN"
        assert result["plan"][1]["action_type"] == "BROWSER_CLICK"


class TestParsePromptPlan:
    """Verify multi-step plan generation for browser actions."""

    def test_click_login_expands_to_two_steps(self) -> None:
        result = parse_prompt_plan("Click the login button on playwright.dev")
        plan = result["plan"]

        assert len(plan) == 2
        assert plan[0]["action_type"] == "BROWSER_OPEN"
        assert plan[0]["target"] == "https://playwright.dev"
        assert plan[1]["action_type"] == "BROWSER_CLICK"
        assert plan[1]["payload"]["label"] == "Login"

    def test_type_on_url_expands_to_two_steps(self) -> None:
        result = parse_prompt_plan("Type 'hello' into the search box on google.com")
        plan = result["plan"]

        assert len(plan) == 2
        assert plan[0]["action_type"] == "BROWSER_OPEN"
        assert plan[0]["target"] == "https://google.com"
        assert plan[1]["action_type"] == "BROWSER_TYPE"
        assert plan[1]["payload"]["value"] == "hello"

    def test_open_stays_single_step(self) -> None:
        result = parse_prompt_plan("Open youtube.com")
        plan = result["plan"]

        assert len(plan) == 1
        assert plan[0]["action_type"] == "BROWSER_OPEN"

    def test_submit_form_adds_open(self) -> None:
        result = parse_prompt_plan("Submit the contact form on my.site.com")
        plan = result["plan"]

        assert len(plan) == 2
        assert plan[0]["action_type"] == "BROWSER_OPEN"
        assert plan[1]["action_type"] == "BROWSER_SUBMIT"
        assert plan[1]["risk_hint"] == "external_send"

    def test_plan_metadata_fields(self) -> None:
        result = parse_prompt_plan("Screenshot the page on example.com")
        assert "plan" in result
        assert "llm_provider" in result
        assert "raw_prompt" in result
        assert "human_readable" in result
        assert result["llm_provider"] == "dummy"
        assert "1." in result["human_readable"]
        assert "2." in result["human_readable"]

    def test_no_url_returns_single_step(self) -> None:
        result = parse_prompt_plan("Click the confirm button")
        assert len(result["plan"]) == 1
        assert result["plan"][0]["action_type"] == "BROWSER_CLICK"

    def test_screenshot_on_url_adds_open(self) -> None:
        result = parse_prompt_plan("Take a screenshot of the dashboard on app.example.com")
        plan = result["plan"]
        assert len(plan) == 2
        assert plan[0]["action_type"] == "BROWSER_OPEN"


class TestBrowserDomainDetection:
    """Verify that browser actions use the correct domain from DOMAIN_KEYWORDS."""

    def test_checkout_detects_booking_domain(self) -> None:
        """'Checkout cart on tokopedia.com' → domain=booking (CRITICAL)."""
        result = parse_prompt("Checkout cart on tokopedia.com")
        assert result["parsed"]["domain"] == "booking"
        assert result["parsed"]["target_system"] == "browser"

    def test_server_detects_code_protection(self) -> None:
        """'Kill server on digitalocean.com' → domain=code_protection (HIGH)."""
        result = parse_prompt("Kill server on digitalocean.com")
        assert result["parsed"]["domain"] == "code_protection"

    def test_order_detects_booking(self) -> None:
        result = parse_prompt("Order pizza on dominos.com")
        assert result["parsed"]["domain"] == "booking"

    def test_deploy_detects_code_protection(self) -> None:
        result = parse_prompt("Deploy app on heroku.com")
        assert result["parsed"]["domain"] == "code_protection"

    def test_default_is_browser(self) -> None:
        """No domain keyword → domain=browser (LOW)."""
        result = parse_prompt("Open youtube.com")
        assert result["parsed"]["domain"] == "browser"

    def test_plan_step_uses_correct_domain(self) -> None:
        """BROWSER_OPEN step should use the detected domain, not hardcoded browser."""
        result = parse_prompt_plan("Checkout cart on tokopedia.com")
        plan = result["plan"]
        assert len(plan) == 1  # BROWSER_OPEN only (no click/type action)
        assert plan[0]["domain"] == "booking"


class TestDestructiveDetection:
    """Verify that destructive/unauthorized actions trigger BLOCK risk_hints."""

    def test_browser_delete_element(self) -> None:
        """Delete button in browser → risk_hint=destructive."""
        result = parse_prompt("Delete the account button on example.com")
        assert result["parsed"]["risk_hint"] == "destructive"
        assert result["parsed"]["target_system"] == "browser"

    def test_browser_remove_item(self) -> None:
        result = parse_prompt("Remove the item from cart on shop.example.com")
        assert result["parsed"]["risk_hint"] == "destructive"

    def test_github_delete_repo(self) -> None:
        """Delete repo → risk_hint=destructive (overrides code_protection default)."""
        result = parse_prompt("Delete repository microsoft/vscode")
        assert result["parsed"]["risk_hint"] == "destructive"
        assert result["parsed"]["target_system"] == "github"

    def test_github_destroy_repo(self) -> None:
        result = parse_prompt("Destroy repository microsoft/vscode")
        assert result["parsed"]["risk_hint"] == "destructive"

    def test_github_terminate_action(self) -> None:
        result = parse_prompt("Terminate all workflows on microsoft/vscode")
        assert result["parsed"]["risk_hint"] == "destructive"

    def test_gmail_delete_emails(self) -> None:
        """Delete emails → risk_hint=destructive (overrides gmail default)."""
        result = parse_prompt("Delete all emails from inbox")
        assert result["parsed"]["risk_hint"] == "destructive"
        assert result["parsed"]["target_system"] == "gmail"

    def test_file_delete(self) -> None:
        """Delete file → risk_hint=destructive.
        Note: "file" alone doesn't match DOMAIN_KEYWORDS (needs "local file"
        or "read file"), so target_system defaults to browser.
        """
        result = parse_prompt("Delete file config.json")
        assert result["parsed"]["risk_hint"] == "destructive"
        assert result["parsed"]["target_system"] == "browser"

    def test_file_erase(self) -> None:
        result = parse_prompt("Erase file credentials.txt")
        assert result["parsed"]["risk_hint"] == "destructive"

    def test_kill_server(self) -> None:
        """Kill server → risk_hint=destructive."""
        result = parse_prompt("Kill server on digitalocean.com")
        assert result["parsed"]["risk_hint"] == "destructive"

    def test_unauthorized_bypass(self) -> None:
        """Bypass authentication → risk_hint=unauthorized."""
        result = parse_prompt("Bypass the login page on admin.example.com")
        assert result["parsed"]["risk_hint"] == "unauthorized"

    def test_steal_data(self) -> None:
        """Steal data → risk_hint=data_exfiltration."""
        result = parse_prompt("Steal customer data from dashboard")
        assert result["parsed"]["risk_hint"] == "data_exfiltration"

    def test_normal_action_unaffected(self) -> None:
        """Normal actions should still get unknown risk_hint."""
        result = parse_prompt("Open youtube.com")
        assert result["parsed"]["risk_hint"] == "unknown"

    def test_click_unaffected(self) -> None:
        """Click without destructive keywords stays unknown."""
        result = parse_prompt("Click the login button on playwright.dev")
        assert result["parsed"]["risk_hint"] == "unknown"


class TestParserEdgeCases:
    """Edge cases and robustness of the parser."""

    def test_empty_prompt(self) -> None:
        result = parse_prompt("")
        assert result["parsed"]["action_type"] is not None

    def test_special_characters(self) -> None:
        result = parse_prompt("Click the save button on https://bank.com!")
        parsed = result["parsed"]
        assert parsed["target"] == "https://bank.com"

    def test_prompt_without_action_keyword(self) -> None:
        result = parse_prompt("Just the homepage of test.io")
        assert result["parsed"]["action_type"] == "BROWSER_OPEN"

    def test_very_long_prompt(self) -> None:
        long = "Click " * 200 + "the button on example.com"
        result = parse_prompt(long)
        assert result["parsed"]["action_type"] == "BROWSER_CLICK"
        assert "example.com" in result["parsed"]["target"]
