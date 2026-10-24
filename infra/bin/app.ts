#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { CatalogBridgeStack } from '../lib/catalogbridge-stack';
import { CicdStack } from '../lib/cicd-stack';

const app = new App();
const ctx = (k: string) => app.node.tryGetContext(k);
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION ?? 'us-west-2' };
const stage = String(ctx('stage') ?? 'dev');

new CatalogBridgeStack(app, 'CatalogBridge', {
  env,
  stage,
  alertEmail: ctx('alertEmail') || undefined,
  workerBatchSize: Number(ctx('workerBatchSize') ?? 10),
  workerMaxConcurrency: Number(ctx('workerMaxConcurrency') ?? 2),
  workerBatchWindowSeconds: Number(ctx('workerBatchWindowSeconds') ?? 0),
  faultInjection: String(ctx('faultInjection')) === 'true',
  bedrockModelId: String(ctx('bedrockModelId')),
  budgetUsd: Number(ctx('budgetUsd') ?? 20),
  description: 'CatalogBridge: serverless CSV catalog normalization (demo MVP)',
});

new CicdStack(app, 'CatalogBridgeCicd', {
  env,
  githubRepo: String(ctx('githubRepo')),
  githubBranch: String(ctx('githubBranch') ?? 'main'),
  existingOidcProviderArn: ctx('githubOidcProviderArn') || undefined,
});
