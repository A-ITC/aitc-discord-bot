# AWS Architecture Icons

`system-architecture.svg` に使用するAWSアイコンの原本です。図は画像を内包しており、外部ファイルやネットワーク接続なしで表示できます。アイコンの形・色は変更せず、表示サイズだけを調整しています。

- API Gateway、Lambda、Systems Manager、KMS、CloudWatch、IAM、CloudFormation、S3、Clientは、`aitc-auth-api/docs/architecture-icons` の2026年7月版AWS公式SVGを再利用しています。
- DynamoDBとAWS Cloudグループは、同じ2026年7月版を使用する `aitc-web-infra/docs/system-architecture.svg` から再利用しています。
- 配布元：[AWS Architecture Icons](https://aws.amazon.com/architecture/icons/)
- 2026年7月版：[Icon-package_07312026.zip](https://d1.awsstatic.com/onedam/marketing-channels/website/public/shared/architecture-icon-release/Icon-package_07312026.5846e92413caa21490223536cc97f1269e44fa92.zip)

SQSとEventBridge Schedulerは、AWS Labs公開リポジトリ `awslabs/aws-icons-for-plantuml` のv23.1からPNGを再利用しています。SVG内にも同じPNGを埋め込みます。

- [SimpleQueueService.puml](https://github.com/awslabs/aws-icons-for-plantuml/blob/v23.1/dist/ApplicationIntegration/SimpleQueueService.puml) — blob `5bf951650e293b1ee588df3b20ab0d8c01d57404`
- [EventBridgeScheduler.puml](https://github.com/awslabs/aws-icons-for-plantuml/blob/v23.1/dist/ApplicationIntegration/EventBridgeScheduler.puml) — blob `6c3a7cd503b022016bf112cba582baa144bc0583`
- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
- アイコンのライセンス：[CC BY-ND 2.0](https://creativecommons.org/licenses/by-nd/2.0/)／[AWS LabsのLICENSE](https://github.com/awslabs/aws-icons-for-plantuml/blob/v23.1/LICENSE)

AWSのアイコン・商標に関する権利はAWSに帰属します。ClientアイコンはDiscordと運用端末を示す汎用記号であり、それらがAWSサービスであることを示すものではありません。
