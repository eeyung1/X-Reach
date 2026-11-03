import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "X-Reach — Draft lab",
  description: "Review a draft and its media before publishing on X.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
