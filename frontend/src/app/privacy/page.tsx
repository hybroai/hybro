import type { Metadata } from "next"
import Link from "next/link"

export const metadata: Metadata = {
  title: "Privacy Policy – Hybro AI",
  description: "Privacy Policy for Hybro AI",
}

export default function PrivacyPage() {
  return (
    <main className="mx-auto max-w-3xl px-6 py-16 text-sm text-foreground">
      <Link href="/" className="text-muted-foreground hover:text-foreground transition-colors text-xs">
        ← Back to Hybro AI
      </Link>

      <h1 className="mt-8 text-3xl font-semibold tracking-tight">Privacy Policy</h1>
      <p className="mt-2 text-muted-foreground">Last updated: November 2026</p>

      <Section title="1. Overview">
        <p>
          Hybro AI ("we", "our", or "us") operates the Hybro AI platform accessible at this website. This Privacy
          Policy explains how we collect, use, and protect information about you when you use our service.
        </p>
      </Section>

      <Section title="2. Information We Collect">
        <p>We collect the following categories of information:</p>
        <ul className="mt-3 list-disc pl-5 space-y-2">
          <li>
            <strong>Local identity metadata</strong> — the self-hosted frontend uses a fixed local identity and does not
            send authentication credentials to a third-party identity provider.
          </li>
          <li>
            <strong>Usage data</strong> — pages visited, features used, and interactions with the platform, collected
            via Google Analytics only when this installation has analytics enabled and you accept it.
          </li>
          <li>
            <strong>Agent and conversation data</strong> — messages and task data you create while using the platform.
          </li>
        </ul>
      </Section>

      <Section title="3. Cookies">
        <p>We use the following types of cookies:</p>
        <ul className="mt-3 list-disc pl-5 space-y-2">
          <li>
            <strong>Essential local storage</strong> — used for interface preferences and cookie-consent state. This
            data remains in your browser.
          </li>
          <li>
            <strong>Analytics cookies</strong> — set by Google Analytics to help us understand how visitors use the
            platform. These are only placed after you accept them in the cookie banner. Installations without
            analytics enabled show no banner and place no analytics cookies.
          </li>
        </ul>
        <p className="mt-3">
          You can withdraw your analytics consent at any time by clearing your browser's local storage or site data for
          this domain.
        </p>
      </Section>

      <Section title="4. Google Analytics">
        <p>
          Analytics is off by default. It is on only when the operator of this installation sets a Google Analytics 4
          measurement id. Then, with your consent, Google Analytics 4 collects usage statistics, and Google may process
          this data on servers outside your country. For more information, see{" "}
          <a
            href="https://policies.google.com/privacy"
            target="_blank"
            rel="noopener noreferrer"
            className="underline underline-offset-4 hover:text-foreground transition-colors"
          >
            Google's Privacy Policy
          </a>
          .
        </p>
        <p className="mt-3">
          The Google Analytics script is not loaded until you accept cookies. If you decline, or analytics is not
          enabled, no data is sent to Google.
        </p>
      </Section>

      <Section title="5. Local Identity">
        <p>
          The current self-hosted frontend operates with a local development identity. It does not create external
          user accounts or send authentication credentials to a third-party identity service.
        </p>
      </Section>

      <Section title="6. Data Retention">
        <p>
          We retain conversation and agent data for as long as needed to operate the service. You can request deletion
          of associated data by contacting us.
        </p>
      </Section>

      <Section title="7. Your Rights">
        <p>Depending on your location, you may have the right to:</p>
        <ul className="mt-3 list-disc pl-5 space-y-2">
          <li>Access the personal data we hold about you</li>
          <li>Request correction or deletion of your data</li>
          <li>Object to or restrict certain processing</li>
          <li>Withdraw consent for analytics cookies at any time</li>
          <li>Lodge a complaint with your local data protection authority</li>
        </ul>
      </Section>

      <Section title="8. Contact">
        <p>
          If you have questions about this Privacy Policy or want to exercise your rights, please contact us at{" "}
          <a
            href="mailto:privacy@hybro.ai"
            className="underline underline-offset-4 hover:text-foreground transition-colors"
          >
            privacy@hybro.ai
          </a>
          .
        </p>
      </Section>
    </main>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-10">
      <h2 className="text-lg font-semibold">{title}</h2>
      <div className="mt-3 space-y-3 leading-6 text-muted-foreground">{children}</div>
    </section>
  )
}
