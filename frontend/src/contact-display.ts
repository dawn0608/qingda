export type ContactChannel = "phone" | "email";

export function maskedContact(value: string, channel: ContactChannel) {
  if (!value) return "未绑定";
  if (channel === "phone") return value.slice(0, 3) + "****" + value.slice(-4);
  const [name, domain] = value.split("@");
  return name.slice(0, 1) + "***@" + domain;
}
