import "dotenv/config";
import express from "express";
import cors from "cors";
import { PrismaClient } from "@prisma/client";

const prisma = new PrismaClient();
const app = express();
const port = Number(process.env.PORT || 3000);
const host = process.env.HOST || "0.0.0.0";
const uploadToken = process.env.UPLOAD_TOKEN || "";

app.use(cors());
app.use(express.json({ limit: "5mb" }));

function requireUploadToken(req, res, next) {
  if (!uploadToken) {
    return res.status(500).json({ ok: false, error: "UPLOAD_TOKEN is not configured" });
  }

  const auth = req.headers.authorization || "";
  if (auth !== `Bearer ${uploadToken}`) {
    return res.status(401).json({ ok: false, error: "Unauthorized" });
  }

  next();
}

app.get("/health", async (_req, res) => {
  try {
    await prisma.$queryRaw`SELECT 1`;
    res.json({ ok: true, db: true });
  } catch (error) {
    res.status(500).json({ ok: false, db: false, error: error.message });
  }
});

app.post("/upload-accounts", requireUploadToken, async (req, res) => {
  const { machineId, accounts } = req.body || {};

  if (!machineId || typeof machineId !== "string") {
    return res.status(400).json({ ok: false, error: "machineId is required" });
  }

  if (!Array.isArray(accounts)) {
    return res.status(400).json({ ok: false, error: "accounts must be an array" });
  }

  const saved = [];
  for (const account of accounts) {
    if (!account || typeof account.fileName !== "string" || account.data === undefined) {
      return res.status(400).json({ ok: false, error: "each account needs fileName and data" });
    }

    const row = await prisma.projektHardAccountBackup.upsert({
      where: {
        machineId_fileName: {
          machineId,
          fileName: account.fileName,
        },
      },
      update: {
        data: account.data,
        fileSize: Number.isInteger(account.fileSize) ? account.fileSize : null,
        fileMtime: account.fileMtime ? new Date(account.fileMtime) : null,
        source: typeof account.source === "string" ? account.source : null,
      },
      create: {
        machineId,
        fileName: account.fileName,
        data: account.data,
        fileSize: Number.isInteger(account.fileSize) ? account.fileSize : null,
        fileMtime: account.fileMtime ? new Date(account.fileMtime) : null,
        source: typeof account.source === "string" ? account.source : null,
      },
      select: {
        id: true,
        fileName: true,
        updatedAt: true,
      },
    });

    saved.push(row);
  }

  res.json({ ok: true, savedCount: saved.length, saved });
});

process.on("SIGINT", async () => {
  await prisma.$disconnect();
  process.exit(0);
});

app.listen(port, host, () => {
  console.log(`PrivateFish backend listening on http://${host}:${port}`);
});
