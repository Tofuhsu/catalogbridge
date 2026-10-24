import * as path from 'path';
import * as fs from 'fs';
import { Duration, RemovalPolicy, Stack, StackProps, CfnOutput, Tags } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as apigw from 'aws-cdk-lib/aws-apigatewayv2';
import * as apigwAuth from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import * as apigwInt from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subs from 'aws-cdk-lib/aws-sns-subscriptions';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import { Monitoring } from './monitoring';

export interface CatalogBridgeProps extends StackProps {
  stage: string;
  alertEmail?: string;
  workerBatchSize: number;
  workerMaxConcurrency: number;
  workerBatchWindowSeconds: number;
  faultInjection: boolean;
  bedrockModelId: string;
  budgetUsd: number;
}

// cdk always runs from infra/, so the repo root is its parent.
const REPO_ROOT = path.resolve(process.cwd(), '..');
const WORKER_TIMEOUT_S = 30;
const IMPORTER_TIMEOUT_S = 60;

export class CatalogBridgeStack extends Stack {
  constructor(scope: Construct, id: string, props: CatalogBridgeProps) {
    super(scope, id, props);
    Tags.of(this).add('Project', 'catalogbridge');
    Tags.of(this).add('Stage', props.stage);
    const prefix = `catalogbridge-${props.stage}`;

    // ------------------------------------------------------------ frontend hosting
    const siteBucket = new s3.Bucket(this, 'SiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
    const securityHeaders = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      securityHeadersBehavior: {
        contentSecurityPolicy: {
          override: true,
          contentSecurityPolicy: [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            `connect-src 'self' https://*.execute-api.${this.region}.amazonaws.com https://*.amazoncognito.com https://*.s3.amazonaws.com https://*.s3.${this.region}.amazonaws.com https://s3.${this.region}.amazonaws.com`,
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self' https://*.amazoncognito.com",
          ].join('; '),
        },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.NO_REFERRER, override: true },
        strictTransportSecurity: { accessControlMaxAge: Duration.days(365), includeSubdomains: true, override: true },
      },
    });
    const distribution = new cloudfront.Distribution(this, 'Site', {
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy: securityHeaders,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
      defaultRootObject: 'index.html',
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: '/index.html', ttl: Duration.seconds(0) },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: '/index.html', ttl: Duration.seconds(0) },
      ],
    });
    const siteUrl = `https://${distribution.distributionDomainName}`;
    const devOrigin = 'http://localhost:5173';

    // ------------------------------------------------------------ storage
    const rawBucket = new s3.Bucket(this, 'RawBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true, // jobs bind to an immutable object version
      eventBridgeEnabled: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      cors: [{
        allowedMethods: [s3.HttpMethods.POST],
        allowedOrigins: [siteUrl, devOrigin],
        allowedHeaders: ['*'],
        maxAge: 300,
      }],
      lifecycleRules: [{ expiration: Duration.days(90), noncurrentVersionExpiration: Duration.days(30) }],
    });
    const workBucket = new s3.Bucket(this, 'WorkBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [{ expiration: Duration.days(90) }],
    });

    const jobs = new dynamodb.Table(this, 'Jobs', {
      partitionKey: { name: 'jobId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    jobs.addGlobalSecondaryIndex({
      indexName: 'byOwner',
      partitionKey: { name: 'ownerId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'createdAt', type: dynamodb.AttributeType.NUMBER },
    });
    const results = new dynamodb.Table(this, 'Results', {
      partitionKey: { name: 'jobId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'rowNumber', type: dynamodb.AttributeType.NUMBER },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const templates = new dynamodb.Table(this, 'MappingTemplates', {
      partitionKey: { name: 'ownerId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'templateId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ------------------------------------------------------------ queues
    // Each consumer queue has its own DLQ. EventBridge delivery failures and the
    // stream consumer's failures have separate DLQs too: different failure types.
    const dlq = (id: string) => new sqs.Queue(this, id, {
      retentionPeriod: Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
    });
    const ingestDlq = dlq('IngestDLQ');
    const workDlq = dlq('WorkDLQ');
    const eventDeliveryDlq = dlq('EventDeliveryDLQ');
    const lifecycleDlq = dlq('LifecycleFailureDLQ');

    const ingestQueue = new sqs.Queue(this, 'IngestQueue', {
      visibilityTimeout: Duration.seconds(6 * IMPORTER_TIMEOUT_S),
      retentionPeriod: Duration.days(4),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      deadLetterQueue: { queue: ingestDlq, maxReceiveCount: 5 },
    });
    const workQueue = new sqs.Queue(this, 'WorkQueue', {
      // AWS guidance: at least 6x function timeout + batching window.
      visibilityTimeout: Duration.seconds(6 * WORKER_TIMEOUT_S + props.workerBatchWindowSeconds),
      retentionPeriod: Duration.days(4),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      deadLetterQueue: { queue: workDlq, maxReceiveCount: 3 },
    });

    // ------------------------------------------------------------ functions
    const faultsParam = props.faultInjection
      ? new ssm.StringParameter(this, 'FaultsParam', {
        parameterName: `/${prefix}/faults`,
        stringValue: '{}',
        description: 'TEST ONLY fault injection config. Keep {} for demos.',
      })
      : undefined;

    const code = lambda.Code.fromAsset(path.join(REPO_ROOT, 'backend'), {
      exclude: ['tests', 'tests/**', '.pytest_cache', '**/__pycache__', 'requirements-dev.txt', 'pytest.ini'],
    });
    const commonEnv: Record<string, string> = {
      JOBS_TABLE: jobs.tableName,
      RESULTS_TABLE: results.tableName,
      TEMPLATES_TABLE: templates.tableName,
      RAW_BUCKET: rawBucket.bucketName,
      WORK_BUCKET: workBucket.bucketName,
      METRICS_NAMESPACE: 'CatalogBridge',
      STALL_MINUTES: '10',
    };
    const fn = (id: string, handler: string, timeoutS: number, memory: number, env: Record<string, string> = {}) => {
      const logGroup = new logs.LogGroup(this, `${id}Logs`, {
        retention: logs.RetentionDays.TWO_WEEKS,
        removalPolicy: RemovalPolicy.DESTROY,
      });
      const f = new lambda.Function(this, id, {
        runtime: lambda.Runtime.PYTHON_3_13,
        architecture: lambda.Architecture.ARM_64,
        handler,
        code,
        timeout: Duration.seconds(timeoutS),
        memorySize: memory,
        logGroup,
        environment: { ...commonEnv, SERVICE_NAME: id, ...env },
      });
      if (faultsParam) {
        f.addEnvironment('FAULTS_PARAM', faultsParam.parameterName);
        faultsParam.grantRead(f);
      }
      return f;
    };

    const apiFn = fn('ApiFn', 'api.app.handler', 15, 512, {
      BEDROCK_MODEL_ID: props.bedrockModelId,
      DAILY_JOB_LIMIT: '30',
    });
    jobs.grantReadWriteData(apiFn);
    results.grantReadData(apiFn);
    templates.grantReadWriteData(apiFn);
    rawBucket.grantPut(apiFn, 'raw/*'); // needed to sign presigned POSTs
    rawBucket.grantRead(apiFn, 'raw/*'); // original-file download links
    workBucket.grantRead(apiFn);
    apiFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: [
        `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/${props.bedrockModelId}`,
        'arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-*',
      ],
    }));

    const importerFn = fn('ImporterFn', 'importer.handler.handler', IMPORTER_TIMEOUT_S, 512, {
      WORK_QUEUE_URL: workQueue.queueUrl,
    });
    jobs.grantReadWriteData(importerFn);
    results.grantReadData(importerFn);
    rawBucket.grantRead(importerFn);
    workBucket.grantReadWrite(importerFn);
    workQueue.grantSendMessages(importerFn);
    if (faultsParam) faultsParam.grantWrite(importerFn); // FINALIZE path shares report code

    const workerFn = fn('WorkerFn', 'worker.handler.handler', WORKER_TIMEOUT_S, 256);
    jobs.grantReadWriteData(workerFn);
    results.grantReadWriteData(workerFn);

    const lifecycleFn = fn('LifecycleFn', 'lifecycle.handler.handler', 60, 512, {
      INGEST_QUEUE_URL: ingestQueue.queueUrl,
    });
    jobs.grantReadWriteData(lifecycleFn);
    results.grantReadData(lifecycleFn);
    workBucket.grantReadWrite(lifecycleFn);
    ingestQueue.grantSendMessages(lifecycleFn);
    if (faultsParam) faultsParam.grantWrite(lifecycleFn);

    const stallFn = fn('StallCheckFn', 'lifecycle.handler.stall_check', 60, 256);
    jobs.grantReadData(stallFn);
    new events.Rule(this, 'StallCheckSchedule', {
      schedule: events.Schedule.rate(Duration.minutes(5)),
      targets: [new targets.LambdaFunction(stallFn)],
    });

    // ------------------------------------------------------------ event wiring
    // (1) S3 Object Created -> EventBridge -> IngestQueue (not an ESM).
    new events.Rule(this, 'UploadReadyRule', {
      eventPattern: {
        source: ['aws.s3'],
        detailType: ['Object Created'],
        detail: { bucket: { name: [rawBucket.bucketName] }, object: { key: [{ prefix: 'raw/' }] } },
      },
      targets: [new targets.SqsQueue(ingestQueue, {
        deadLetterQueue: eventDeliveryDlq,
        retryAttempts: 8,
        maxEventAge: Duration.hours(2),
        message: events.RuleTargetInput.fromObject({
          type: 'UPLOAD_READY',
          bucket: events.EventField.fromPath('$.detail.bucket.name'),
          key: events.EventField.fromPath('$.detail.object.key'),
          versionId: events.EventField.fromPath('$.detail.object.version-id'),
          size: events.EventField.fromPath('$.detail.object.size'),
          eventId: events.EventField.fromPath('$.id'),
        }),
      })],
    });

    // (2) IngestQueue -> Importer (SQS event source mapping, partial batch response).
    importerFn.addEventSource(new sources.SqsEventSource(ingestQueue, {
      batchSize: 5,
      reportBatchItemFailures: true,
      maxConcurrency: 2,
    }));

    // (3) WorkQueue -> Worker. batch size / max concurrency come from context so the
    // throughput experiments are reproducible from the CDK command line.
    workerFn.addEventSource(new sources.SqsEventSource(workQueue, {
      batchSize: props.workerBatchSize,
      maxBatchingWindow: props.workerBatchWindowSeconds > 0 ? Duration.seconds(props.workerBatchWindowSeconds) : undefined,
      reportBatchItemFailures: true,
      maxConcurrency: props.workerMaxConcurrency,
    }));

    // (4) Jobs stream -> Lifecycle. Filter keeps only the states it acts on, so its
    // own REPORTING/COMPLETED writes do not trigger it again.
    lifecycleFn.addEventSource(new sources.DynamoEventSource(jobs, {
      startingPosition: lambda.StartingPosition.LATEST,
      batchSize: 100,
      maxBatchingWindow: Duration.seconds(1),
      bisectBatchOnError: true,
      retryAttempts: 5,
      maxRecordAge: Duration.hours(1),
      reportBatchItemFailures: true,
      onFailure: new sources.SqsDlq(lifecycleDlq),
      filters: [lambda.FilterCriteria.filter({
        eventName: lambda.FilterRule.isEqual('MODIFY'),
        dynamodb: { NewImage: { status: { S: lambda.FilterRule.or('RUN_REQUESTED', 'PROCESSING') } } },
      })],
    }));

    // ------------------------------------------------------------ auth
    const userPool = new cognito.UserPool(this, 'Users', {
      selfSignUpEnabled: false, // invited test accounts only
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: { minLength: 12, requireDigits: true, requireLowercase: true, requireUppercase: true, requireSymbols: false },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: RemovalPolicy.DESTROY,
      userInvitation: {
        emailSubject: 'Your CatalogBridge test account',
        // One item per line: a one-line body made the temporary password easy to mis-copy
        // (reported by the first real tester).
        emailBody: '<p>You were invited to try CatalogBridge (a demo project).</p>'
          + '<p>Sign in with <b>the email address this message was sent to</b> (not the account ID below).</p>'
          + '<p>Temporary password (copy exactly, every character counts):</p>'
          + '<p><code style="font-size:16px">{####}</code></p>'
          + `<p>Sign in at <a href="${siteUrl}">${siteUrl}</a>. You will be asked to choose a new password.</p>`
          + '<p style="color:#667085">Account ID (support reference only): {username}</p>',
      },
    });
    const domain = userPool.addDomain('Domain', {
      cognitoDomain: { domainPrefix: `${prefix}-${this.account}` },
    });
    const client = userPool.addClient('SpaClient', {
      generateSecret: false, // public SPA client; PKCE instead of a secret
      authFlows: { userSrp: true },
      preventUserExistenceErrors: true,
      accessTokenValidity: Duration.hours(1),
      idTokenValidity: Duration.hours(1),
      refreshTokenValidity: Duration.days(1),
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
        callbackUrls: [`${siteUrl}/callback`, `${devOrigin}/callback`],
        logoutUrls: [`${siteUrl}/`, `${devOrigin}/`],
      },
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
    });

    // Automation-only client for scripts/e2e.py and benchmarks: AdminInitiateAuth
    // requires IAM admin credentials, so it is not reachable from the browser.
    const testClient = userPool.addClient('AutomationClient', {
      generateSecret: false,
      authFlows: { adminUserPassword: true },
      accessTokenValidity: Duration.hours(1),
      idTokenValidity: Duration.hours(1),
      refreshTokenValidity: Duration.days(1),
    });

    // ------------------------------------------------------------ API
    const httpApi = new apigw.HttpApi(this, 'HttpApi', {
      corsPreflight: {
        allowOrigins: [siteUrl, devOrigin],
        allowMethods: [apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST, apigw.CorsHttpMethod.PUT],
        allowHeaders: ['authorization', 'content-type'],
        maxAge: Duration.minutes(10),
      },
    });
    const authorizer = new apigwAuth.HttpJwtAuthorizer('Jwt', userPool.userPoolProviderUrl, {
      jwtAudience: [client.userPoolClientId, testClient.userPoolClientId],
    });
    const integration = new apigwInt.HttpLambdaIntegration('ApiIntegration', apiFn);
    const routes: Array<[apigw.HttpMethod, string]> = [
      [apigw.HttpMethod.POST, '/jobs'],
      [apigw.HttpMethod.GET, '/jobs'],
      [apigw.HttpMethod.GET, '/jobs/{jobId}'],
      [apigw.HttpMethod.GET, '/jobs/{jobId}/preview'],
      [apigw.HttpMethod.POST, '/jobs/{jobId}/mapping-suggestion'],
      [apigw.HttpMethod.PUT, '/jobs/{jobId}/mapping'],
      [apigw.HttpMethod.POST, '/jobs/{jobId}/run'],
      [apigw.HttpMethod.GET, '/jobs/{jobId}/results'],
      [apigw.HttpMethod.GET, '/jobs/{jobId}/downloads'],
      [apigw.HttpMethod.GET, '/mapping-templates'],
      [apigw.HttpMethod.PUT, '/mapping-templates/{templateId}'],
    ];
    for (const [method, p] of routes) {
      httpApi.addRoutes({ path: p, methods: [method], integration, authorizer });
    }
    const stage = httpApi.defaultStage!.node.defaultChild as apigw.CfnStage;
    stage.defaultRouteSettings = { throttlingRateLimit: 20, throttlingBurstLimit: 40 };

    // ------------------------------------------------------------ site deploy
    const distDir = path.join(REPO_ROOT, 'frontend', 'dist');
    if (fs.existsSync(distDir)) {
      new s3deploy.BucketDeployment(this, 'DeploySite', {
        destinationBucket: siteBucket,
        sources: [
          s3deploy.Source.asset(distDir),
          s3deploy.Source.jsonData('config.json', {
            region: this.region,
            apiUrl: httpApi.apiEndpoint,
            userPoolId: userPool.userPoolId,
            clientId: client.userPoolClientId,
            cognitoDomain: `${domain.domainName}.auth.${this.region}.amazoncognito.com`,
          }),
        ],
        distribution,
        distributionPaths: ['/*'],
        memoryLimit: 256,
      });
    }

    // ------------------------------------------------------------ monitoring + cost
    const topic = new sns.Topic(this, 'Alerts', { displayName: 'CatalogBridge alerts' });
    if (props.alertEmail) topic.addSubscription(new subs.EmailSubscription(props.alertEmail));
    new Monitoring(this, 'Monitoring', {
      topic,
      functions: { Api: apiFn, Importer: importerFn, Worker: workerFn, Lifecycle: lifecycleFn, StallCheck: stallFn },
      ingestQueue, workQueue,
      dlqs: { IngestDLQ: ingestDlq, WorkDLQ: workDlq, EventDeliveryDLQ: eventDeliveryDlq, LifecycleFailureDLQ: lifecycleDlq },
      httpApi,
    });

    if (props.alertEmail) {
      const subscribers = [{ subscriptionType: 'EMAIL', address: props.alertEmail }];
      new budgets.CfnBudget(this, 'Budget', {
        budget: {
          budgetName: `${prefix}-monthly`,
          budgetType: 'COST',
          timeUnit: 'MONTHLY',
          budgetLimit: { amount: props.budgetUsd, unit: 'USD' },
        },
        notificationsWithSubscribers: [
          { notification: { notificationType: 'ACTUAL', comparisonOperator: 'GREATER_THAN', threshold: 50, thresholdType: 'PERCENTAGE' }, subscribers },
          { notification: { notificationType: 'ACTUAL', comparisonOperator: 'GREATER_THAN', threshold: 80, thresholdType: 'PERCENTAGE' }, subscribers },
          { notification: { notificationType: 'FORECASTED', comparisonOperator: 'GREATER_THAN', threshold: 100, thresholdType: 'PERCENTAGE' }, subscribers },
        ],
      });
    }

    // ------------------------------------------------------------ outputs (read by scripts/)
    const out = (k: string, v: string) => new CfnOutput(this, `Out${k}`, { key: k, value: v });
    out('SiteUrl', siteUrl);
    out('ApiUrl', httpApi.apiEndpoint);
    out('UserPoolId', userPool.userPoolId);
    out('UserPoolClientId', client.userPoolClientId);
    out('AutomationClientId', testClient.userPoolClientId);
    out('CognitoDomain', `${domain.domainName}.auth.${this.region}.amazoncognito.com`);
    out('JobsTable', jobs.tableName);
    out('ResultsTable', results.tableName);
    out('RawBucket', rawBucket.bucketName);
    out('WorkBucket', workBucket.bucketName);
    out('IngestQueueUrl', ingestQueue.queueUrl);
    out('WorkQueueUrl', workQueue.queueUrl);
    out('WorkDLQUrl', workDlq.queueUrl);
    out('IngestDLQUrl', ingestDlq.queueUrl);
    out('EventDeliveryDLQUrl', eventDeliveryDlq.queueUrl);
    out('LifecycleDLQUrl', lifecycleDlq.queueUrl);
    out('WorkerFunctionName', workerFn.functionName);
    out('ImporterFunctionName', importerFn.functionName);
    out('LifecycleFunctionName', lifecycleFn.functionName);
    out('ApiFunctionName', apiFn.functionName);
    out('AlertsTopicArn', topic.topicArn);
    if (faultsParam) out('FaultsParam', faultsParam.parameterName);
  }
}
