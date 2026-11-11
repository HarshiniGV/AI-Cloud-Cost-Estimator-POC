import "./globals.css";

export const metadata = {
  title: "Cloud Cost Estimator",
  description: "Compare Azure, OCI, AWS, and GCP monthly cost estimates side by side",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}