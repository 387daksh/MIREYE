import createClient from "openapi-fetch";
import type { paths } from "./api.generated";
export const api = createClient<paths>({ baseUrl: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000" });
