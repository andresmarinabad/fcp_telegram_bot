import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "FCP — Calendari",
  description: "Calendari i resultats de l'equip",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="ca"><body>{children}</body></html>;
}
