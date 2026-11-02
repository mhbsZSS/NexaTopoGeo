# NexaTopoGeo
**Plataforma B2B para automação de peças técnicas, mapas e gestão em nuvem para escritórios de agrimensura.**

## 1. A dor
Escritórios de agrimensura perdem dezenas de horas mensais formatando relatórios manualmente a partir de dados brutos e gerando croquis do zero no AutoCAD. Esse processo é lento, descentraliza arquivos pesados (DWG, RTK) em pen-drives e aumenta o risco de erros que causam devoluções em cartórios.

## 2. Persona
**Carlos, 42 anos, dono de um escritório de topografia.** Passa o dia em campo medindo propriedades e, ao voltar, precisa varar a noite formatando planilhas do SIGEF em memoriais descritivos no Word e desenhando polígonos no AutoCAD para não perder os prazos dos clientes.

## 3. Concorrentes e alternativas
*   **O "jeito de sempre":** Digitação manual do SIGEF para o Word e desenho do zero no AutoCAD (alto risco de erro e extremo desperdício de tempo).
*   **Planilhas de Excel:** Macros adaptadas que formatam texto, mas quebram frequentemente e não geram mapas.
*   **Softwares Desktop (ex: Métrica TOPO):** Robustos, mas caros, presos a uma máquina local e com curva de aprendizado complexa.

## 4. Proposta de valor
Automatize a geração de memoriais descritivos e croquis base (DXF) direto do navegador, integrando gestão de prazos e armazenamento em nuvem em uma única plataforma SaaS.

## 5. Modelo de receita com Stripe
*   **Plano Free:** Uso gratuito para 2 projetos/mês, com PDFs gerados contendo marca d'água da plataforma.
*   **Plano Pro (R$ 149,90/mês):** Geração ilimitada de relatórios sem marca d'água, processamento de croquis automatizados (DXF) e storage de arquivos pesados (DWG/RINEX) em nuvem.

## 6. Métricas de sucesso
*   **MRR:** Receita Recorrente Mensal via Stripe.
*   **Projetos Processados:** Volume de dados brutos convertidos em peças técnicas com sucesso.
*   **Retenção:** Escritórios que mantêm a assinatura ativa após o 3º mês de uso.

## 7. Lean Canvas

| BLOCO | RESPOSTA |
| :--- | :--- |
| **Problema** | 1. Arquivos pesados perdidos. 2. Horas gastas desenhando croquis base. 3. Formatação manual de memoriais sujeita a erros. |
| **Segmentos** | Empresas de topografia, engenheiros e técnicos independentes. |
| **Proposta de valor** | Única plataforma web que integra geração automática de mapas/memoriais com storage de engenharia. |
| **Solução** | 1. Extração de dados brutos para gerar croquis (DXF) e relatórios. 2. Kanban de projetos. 3. Storage para CAD/Fotos. |
| **Canais** | Redes sociais focadas em engenharia, tráfego pago B2B, LinkedIn. |
| **Fontes de receita** | Assinatura SaaS mensal (Stripe). |
| **Estrutura de custos** | Servidores Docker, MinIO (Storage), MariaDB, taxas Stripe. |
| **Métricas-chave** | MRR, Churn Rate, Terabytes de storage utilizados. |
| **Vantagem injusta** | Motor próprio (Python/ezdxf) que elimina a necessidade de desenhar polígonos iniciais no AutoCAD. |

## 8. Mapa de Reaproveitamento da Arquitetura

| PEÇA DO PROJETO ATUAL | O QUE VIRA NO SAAS NEXATOPO |
| :--- | :--- |
| **Catálogo de filmes (TMDB)** | **Painel Kanban:** Gestão visual de clientes e status dos processos no cartório. |
| **Microsserviço de login + RBAC** | **Níveis de Acesso:** `admin` (Engenheiro RT) e `usuario` (Técnico de campo). |
| **Logs e auditoria (Redis)** | **Rastreio:** Log de envios para o cartório e edições em memoriais. |
| **Upload e perfil (MinIO)** | **Storage Especializado:** Upload de planilhas SIGEF, DWGs pesados e fotos de marcos. |
| **Plano Premium (Stripe)** | **Liberação Pro:** Desbloqueia a geração ilimitada de croquis e relatórios. |