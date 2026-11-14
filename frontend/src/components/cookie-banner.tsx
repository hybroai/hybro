"use client"

import Script from "next/script"
import { useEffect, useRef, useState } from "react"
import { publicConfig } from "@/lib/config"

const CONSENT_KEY = "cookie_consent"

/**
 * Opt-in Google Analytics. Without a configured measurement id there is no
 * script and no banner. With one, the script loads only after the user accepts.
 */
export function CookieBanner() {
  const measurementId = publicConfig().analytics_measurement_id
  const [show, setShow] = useState(false)
  const [accepted, setAccepted] = useState(false)
  const bannerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!measurementId) return
    const stored = localStorage.getItem(CONSENT_KEY)
    if (stored === "accepted") {
      setAccepted(true)
    } else if (stored !== "declined") {
      setShow(true)
    }
  }, [measurementId])

  useEffect(() => {
    document.body.dataset.cookieBanner = show ? "visible" : "hidden"
    const banner = bannerRef.current
    if (!show || !banner) {
      document.documentElement.style.removeProperty("--cookie-banner-height")
      return () => {
        delete document.body.dataset.cookieBanner
      }
    }

    const updateInset = () => {
      document.documentElement.style.setProperty(
        "--cookie-banner-height",
        `${banner.getBoundingClientRect().height}px`,
      )
    }
    updateInset()
    const observer = new ResizeObserver(updateInset)
    observer.observe(banner)
    return () => {
      observer.disconnect()
      document.documentElement.style.removeProperty("--cookie-banner-height")
      delete document.body.dataset.cookieBanner
    }
  }, [show])

  const accept = () => {
    localStorage.setItem(CONSENT_KEY, "accepted")
    setShow(false)
    setAccepted(true)
  }

  const decline = () => {
    localStorage.setItem(CONSENT_KEY, "declined")
    setShow(false)
  }

  if (accepted && measurementId) {
    return (
      <>
        <Script
          src={`https://www.googletagmanager.com/gtag/js?id=${measurementId}`}
          strategy="afterInteractive"
        />
        <Script id="google-analytics" strategy="afterInteractive">
          {`
            window.dataLayer = window.dataLayer || [];
            function gtag(){dataLayer.push(arguments);}
            gtag('js', new Date());
            gtag('config', ${JSON.stringify(measurementId)});
          `}
        </Script>
      </>
    )
  }

  if (!show) return null

  return (
    <div ref={bannerRef} className="fixed bottom-0 inset-x-0 z-60 border-t bg-background px-4 py-3 shadow-lg sm:py-4 sm:flex sm:items-center sm:justify-between sm:gap-6 pb-[calc(0.75rem+env(safe-area-inset-bottom))] sm:pb-[calc(1rem+env(safe-area-inset-bottom))]">
      <p className="text-sm text-muted-foreground">
        We use cookies to analyze traffic via Google Analytics. See our{" "}
        <a href="/privacy" className="underline underline-offset-4 hover:text-foreground transition-colors">
          Privacy Policy
        </a>
        .
      </p>
      <div className="mt-2 flex shrink-0 gap-2 sm:mt-0">
        <button
          onClick={decline}
          className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted transition-colors"
        >
          Decline
        </button>
        <button
          onClick={accept}
          className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 transition-colors"
        >
          Accept
        </button>
      </div>
    </div>
  )
}
