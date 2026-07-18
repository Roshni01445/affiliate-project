from playwright.sync_api import sync_playwright


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print("Opening Gemini... Please log into your Google Account manually now.")
        page.goto("https://gemini.google.com/app")

        input("Press ENTER in this terminal AFTER you have successfully signed in and see the Gemini chat box...")

        context.storage_state(path="auth.json")
        print("✅ Success! auth.json has been created locally. You can close the browser.")
        browser.close()


if __name__ == "__main__":
    run()