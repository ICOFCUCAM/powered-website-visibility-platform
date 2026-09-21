import "./marketing.css";

/**
 * The marketing surface is dark and typographic; the app is light and
 * functional. Two different jobs, deliberately not the same skin — but the
 * same typeface and the same accent, so they read as one product.
 */
export default function MarketingLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <>
      <link
        rel="stylesheet"
        href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&display=swap"
      />
      <div className="mk">{children}</div>
    </>
  );
}
