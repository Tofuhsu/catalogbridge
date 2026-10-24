// Run: npm run build && npm test   (asserts the synthesized CloudFormation template)
const { test } = require('node:test');
const assert = require('node:assert');
const path = require('node:path');
process.chdir(path.join(__dirname, '..'));
const { App } = require('aws-cdk-lib');
const { Template, Match } = require('aws-cdk-lib/assertions');
const { CatalogBridgeStack } = require('../build/lib/catalogbridge-stack');

const app = new App();
const stack = new CatalogBridgeStack(app, 'T', {
  env: { account: '123456789012', region: 'us-west-2' }, stage: 'test', alertEmail: 'a@example.com',
  workerBatchSize: 10, workerMaxConcurrency: 2, workerBatchWindowSeconds: 0, faultInjection: false,
  bedrockModelId: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', budgetUsd: 20,
});
const t = Template.fromStack(stack);

test('no NAT gateway, RDS, or MSK', () => {
  t.resourceCountIs('AWS::EC2::NatGateway', 0);
  t.resourceCountIs('AWS::RDS::DBInstance', 0);
  t.resourceCountIs('AWS::MSK::Cluster', 0);
});

test('worker SQS mapping: partial batch response, batch 10, max concurrency 2', () => {
  t.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
    BatchSize: 10, FunctionResponseTypes: ['ReportBatchItemFailures'], ScalingConfig: { MaximumConcurrency: 2 },
  });
});

test('work queue visibility timeout >= 6x worker timeout, DLQ after 3 receives', () => {
  t.hasResourceProperties('AWS::SQS::Queue', {
    VisibilityTimeout: 180, RedrivePolicy: Match.objectLike({ maxReceiveCount: 3 }),
  });
});

test('stream mapping: filtered, bounded retries, failure destination, bisect', () => {
  t.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
    StartingPosition: 'LATEST', MaximumRetryAttempts: 5, BisectBatchOnFunctionError: true,
    MaximumRecordAgeInSeconds: 3600, FunctionResponseTypes: ['ReportBatchItemFailures'],
    DestinationConfig: Match.objectLike({ OnFailure: Match.anyValue() }),
    FilterCriteria: Match.objectLike({ Filters: Match.anyValue() }),
  });
});

test('raw bucket is versioned, private, and sends EventBridge events', () => {
  t.hasResourceProperties('AWS::S3::Bucket', {
    VersioningConfiguration: { Status: 'Enabled' },
    PublicAccessBlockConfiguration: { BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true },
  });
});

test('raw bucket EventBridge notifications enabled', () => {
  t.hasResourceProperties('Custom::S3BucketNotifications', { NotificationConfiguration: { EventBridgeConfiguration: {} } });
});

test('cognito: no self sign-up, SPA client without secret', () => {
  t.hasResourceProperties('AWS::Cognito::UserPool', { AdminCreateUserConfig: { AllowAdminCreateUserOnly: true } });
  t.hasResourceProperties('AWS::Cognito::UserPoolClient', { GenerateSecret: false, AllowedOAuthFlows: ['code'] });
});

test('fault injection is off unless requested', () => {
  t.resourceCountIs('AWS::SSM::Parameter', 0);
});

test('budget alarm at US$20', () => {
  t.hasResourceProperties('AWS::Budgets::Budget', { Budget: Match.objectLike({ BudgetLimit: { Amount: 20, Unit: 'USD' } }) });
});

test('log retention is set for every function log group', () => {
  const groups = t.findResources('AWS::Logs::LogGroup');
  assert.ok(Object.keys(groups).length >= 5);
  for (const g of Object.values(groups)) assert.strictEqual(g.Properties.RetentionInDays, 14);
});
