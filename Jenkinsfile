pipeline {
    agent {
        label 'devenv'
    }
    parameters {
        booleanParam(name: 'ADD_VERSION_SUFFIX', defaultValue: true, description: 'for dev branches only')
        booleanParam(name: 'UPLOAD_TO_POOL', defaultValue: true, description: 'for all packages')
        string(name: 'WBDEV_IMAGE', defaultValue: '', description: 'docker image path and tag')
    }
    environment {
        WBDEV_BUILD_METHOD = 'sbuild'
        WBDEV_TARGET = 'wb6'
        PROJECT_SUBDIR = 'project'
        RESULT_SUBDIR = 'result'
    }
    options {
        checkoutToSubdirectory('project')
    }
    stages {
        stage('Cleanup workspace') {
            steps {
                cleanWs deleteDirs: true, patterns: [[pattern: "$RESULT_SUBDIR", type: 'INCLUDE']]
            }
        }
        stage('Determine version prefix') {
            when {
                not {
                    branch 'master'
                }
                expression {
                    params.ADD_VERSION_SUFFIX
                }
            }

            steps {
                dir("$PROJECT_SUBDIR") {
                    script {
                        sh 'git config remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*" && git fetch --all'

                        def versionSuffix = sh(returnStdout: true, script:'''\\
                            echo ~`echo $GIT_BRANCH | sed 's/[/~^ \\-\\_]/+/g'`+`\\
                            git log --right-only  --oneline origin/master..HEAD | wc -l`+`\\
                            git rev-parse --short HEAD`''').trim()
                        env.SBUILD_ARGS = "--append-to-version='${versionSuffix}' --maintainer='Robot'"
                    }
                }
            }
        }

        stage('Build package') {
            steps {
                dir("$PROJECT_SUBDIR") {
                    sh 'wbdev ndeb $SBUILD_ARGS'
                }
            }
            post {
                always {
                    sh 'mkdir -p $RESULT_SUBDIR && (find . -maxdepth 1 -type f -exec mv "{}" $RESULT_SUBDIR \\; )'
                    dir("$PROJECT_SUBDIR") {
                        sh 'wbdev root chown -R jenkins:jenkins .'
                    }
                }
                success {
                    archiveArtifacts artifacts: "$RESULT_SUBDIR/*.deb"
                }
            }
        }

        stage('Add packages to pool') {
            when { expression {
                params.UPLOAD_TO_POOL
            }}

            environment {
                APTLY_CONFIG = credentials('release-aptly-config')
            }

            steps {
                sh 'wbci-repo -c $APTLY_CONFIG add-debs -f -d "jenkins:$JOB_NAME.$BUILD_NUMBER" $RESULT_SUBDIR/*.deb'
            }
        }
    
        stage('Upload via wb-releases') {
            when { expression {
                params.UPLOAD_TO_POOL
            }}

            steps {
                build job: 'contactless/wb-releases/master', wait: true
            }
        }
    }
}
